"""Causal ten-venue ensemble challenger, immutable forward score and quote proof.

This is research, never an approved order. Inputs are existing genuine
pre-outcome per-venue predictions with independently observed feed health.
No future candles, retrospectively imported predictions or unverified fills.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL as SOURCE_MODEL
from parlay_bot.perpetuals.fusion import (
    SourceForecast,
    SourceSkill,
    fuse_forecasts,
    utc,
)
from parlay_bot.perpetuals.live_quotes import BookQuote, PerpReferenceBook
from parlay_bot.perpetuals.profit_proof import (
    COST_SCENARIOS,
    ENTRY_GRACE,
    EXIT_GRACE,
    NOTIONAL_USDT,
)
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
MODEL = "ten-venue-fusion-shadow-v1"
VENUES = frozenset({
    "hyperliquid", "dydx", "bitget", "gate", "okx",
    "kucoin", "deribit", "binance", "lighter", "phemex",
})
ASSETS = ("BTC", "ETH", "SOL")
HORIZONS = (5, 15)
MAX_SOURCE_AGE = timedelta(minutes=3)
MIN_LIVE_REMAINING = timedelta(seconds=90)
MIN_QUORUM = 3
MIN_PAIRED_WINDOWS = 150
MIN_DAYS = 21


def _time(value: object) -> datetime:
    return utc(datetime.fromisoformat(str(value)))


def _asset_contract(asset: str, contract: str) -> bool:
    prefix = "XBT" if asset == "BTC" and contract.startswith("XBT") else asset
    if not contract.startswith(prefix):
        return False
    remainder = contract[len(prefix):]
    return remainder == "" or remainder.startswith(("-", "_", "USDT", "USDC", "USD"))


def _can_reference(venue: str, asset: str, contract: str, quote: str) -> bool:
    # Strict quote identity is required only for shadow execution. Other
    # venues can contribute *directional probabilities*, not executable prices.
    if venue == "hyperliquid":
        return contract == asset and quote == "USDC"
    if venue == "binance":
        return contract == asset + "USDT" and quote == "USDT"
    return False


class MultiExchangeResearch:
    """An experimental model which must beat the incumbent on paired targets."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS perp_multi_forward (
                    asset TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    target_opened_at TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    probability_up DOUBLE PRECISION NOT NULL,
                    reference_probability_up DOUBLE PRECISION NOT NULL,
                    reference_venue TEXT NOT NULL,
                    reference_contract TEXT NOT NULL,
                    reference_quote TEXT NOT NULL,
                    reference_opened_at TEXT NOT NULL,
                    contributor_count INTEGER NOT NULL,
                    contributors_json TEXT NOT NULL,
                    settled_at TEXT,
                    outcome_up INTEGER,
                    challenger_brier DOUBLE PRECISION,
                    incumbent_brier DOUBLE PRECISION,
                    PRIMARY KEY (asset,horizon_minutes,target_opened_at)
                );
                CREATE INDEX IF NOT EXISTS idx_perp_multi_pending
                    ON perp_multi_forward (settled_at,target_opened_at);
                CREATE TABLE IF NOT EXISTS perp_multi_quote_shadow (
                    asset TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    target_opened_at TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    entry_at TEXT NOT NULL,
                    entry_bid DOUBLE PRECISION NOT NULL,
                    entry_ask DOUBLE PRECISION NOT NULL,
                    state TEXT NOT NULL,
                    exit_at TEXT,
                    exit_bid DOUBLE PRECISION,
                    exit_ask DOUBLE PRECISION,
                    challenger_gross_bps DOUBLE PRECISION,
                    incumbent_gross_bps DOUBLE PRECISION,
                    PRIMARY KEY (asset,horizon_minutes,target_opened_at)
                );
                CREATE INDEX IF NOT EXISTS idx_perp_multi_quote_pending
                    ON perp_multi_quote_shadow (state,target_opened_at);
                """
            )
        self._initialized = True

    def issue(self, *, now: datetime | None = None) -> int:
        stamp = utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            # Query covers only the most recent model issuance, never archived
            # candles or later settlement. The 10-venue market collector keeps
            # full provenance and recorded failures in perp_feed_health.
            forecasts = conn.execute(
                """SELECT asset,venue,contract,quote,horizon_minutes,
                          reference_opened_at,reference_close,
                          issued_at,target_opened_at,probability_up
                   FROM perp_forward_audit
                   WHERE model_version=? AND settled_at IS NULL
                     AND issued_at<=? AND issued_at>=?
                     AND target_opened_at>=?
                   ORDER BY target_opened_at DESC,issued_at DESC LIMIT 240""",
                (SOURCE_MODEL, stamp.isoformat(),
                 (stamp - MAX_SOURCE_AGE).isoformat(),
                 (stamp - timedelta(minutes=4)).isoformat()),
            ).fetchall()
            feed = conn.execute(
                """SELECT venue,asset,status,last_success_at,last_candle_close
                   FROM perp_feed_health"""
            ).fetchall()
            skill_rows = conn.execute(
                """SELECT asset,venue,horizon_minutes,
                          COUNT(*) AS n,SUM(model_brier) AS loss
                   FROM perp_forward_audit
                   WHERE model_version=? AND settled_at IS NOT NULL
                     AND settled_at<? AND issued_at>=?
                   GROUP BY asset,venue,horizon_minutes""",
                (SOURCE_MODEL, stamp.isoformat(),
                 (stamp - timedelta(days=30)).isoformat()),
            ).fetchall()
        feed_by_key = {(x["venue"], x["asset"]): x for x in feed}
        skill = {
            (x["asset"], x["venue"], int(x["horizon_minutes"])):
                SourceSkill(count=int(x["n"]), brier_sum=float(x["loss"]),
                            recent_count=float(x["n"]),
                            recent_brier_sum=float(x["loss"]))
            for x in skill_rows if x["loss"] is not None
        }
        groups: dict[tuple[str, int, str], dict[str, dict]] = {}
        for source in forecasts:
            row = dict(source)
            asset, venue = str(row["asset"]), str(row["venue"])
            horizon = int(row["horizon_minutes"])
            if (asset not in ASSETS or venue not in VENUES
                or horizon not in HORIZONS
                or not _asset_contract(asset, str(row["contract"]))
                or str(row["quote"]) not in ("USD", "USDT", "USDC")):
                continue
            status = feed_by_key.get((venue, asset))
            if (status is None or status["status"] != "ok"
                or not status["last_success_at"] or not status["last_candle_close"]):
                continue
            if not (timedelta(0) <= stamp - _time(status["last_success_at"])
                    <= timedelta(minutes=15)):
                continue
            if not (timedelta(0) <= stamp - _time(status["last_candle_close"])
                    <= timedelta(minutes=20)):
                continue
            probability = float(row["probability_up"])
            target_open = _time(row["target_opened_at"])
            due = target_open + timedelta(minutes=5)
            if (not math.isfinite(probability) or not 0 < probability < 1
                or due - stamp < MIN_LIVE_REMAINING):
                continue
            key = (asset, horizon, target_open.isoformat())
            venues = groups.setdefault(key, {})
            # Prevent duplicate wrappers of one actual exchange boosting quorum.
            if venue not in venues or row["issued_at"] > venues[venue]["issued_at"]:
                venues[venue] = row

        issued = 0
        for (asset, horizon, target), grouped in sorted(groups.items()):
            if len(grouped) < MIN_QUORUM:
                continue
            candidates = [
                SourceForecast(
                    source=venue, origin=venue, asset=asset,
                    quote="USD-DIRECTION", horizon_minutes=horizon,
                    observed_at=_time(row["issued_at"]),
                    probability_up=float(row["probability_up"]), quality=1.0,
                    reference_price=float(row["reference_close"]),
                )
                for venue, row in sorted(grouped.items())
            ]

            def historical_skill(
                candidate_asset: str, _quote: str, origin: str, candidate_horizon: int,
            ) -> SourceSkill:
                return skill.get(
                    (candidate_asset, origin, candidate_horizon), SourceSkill(),
                )

            combined = fuse_forecasts(
                candidates, issued_at=stamp, skill_for=historical_skill,
                max_age=MAX_SOURCE_AGE,
            )
            if combined is None:
                continue
            # A conventional venue provides the reference outcome and optional
            # same-venue live bid/ask evidence, never another venue's quote.
            ref_venue = next(
                (v for v in ("hyperliquid", "binance")
                 if v in grouped and _can_reference(
                     v, asset, str(grouped[v]["contract"]), str(grouped[v]["quote"])
                 )), min(grouped),
            )
            reference = grouped[ref_venue]
            contributors = json.dumps([
                {
                    "venue": item.origin, "issued_at": utc(item.observed_at).isoformat(),
                    "probability": item.probability_up, "weight": weight,
                }
                for item, weight in zip(combined.contributors, combined.weights)
            ], separators=(",", ":"), allow_nan=False)
            with self.store.connect() as conn:
                result = conn.execute(
                    """INSERT INTO perp_multi_forward (
                       asset,horizon_minutes,target_opened_at,issued_at,
                       model_version,probability_up,reference_probability_up,
                       reference_venue,reference_contract,reference_quote,
                       reference_opened_at,contributor_count,contributors_json
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (asset, horizon, target, stamp.isoformat(), MODEL,
                     combined.probability_up, float(reference["probability_up"]),
                     ref_venue, reference["contract"], reference["quote"],
                     reference["reference_opened_at"], len(combined.contributors),
                     contributors),
                )
                issued += int(result.rowcount == 1)
        return issued

    def settle(self, *, now: datetime | None = None) -> int:
        stamp = utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            due = conn.execute(
                """SELECT e.asset,e.horizon_minutes,e.target_opened_at,
                          e.issued_at,e.probability_up,e.reference_probability_up,
                          a.outcome_up,a.settled_at AS reference_settled_at
                   FROM perp_multi_forward AS e
                   JOIN perp_forward_audit AS a
                     ON a.venue=e.reference_venue
                    AND a.contract=e.reference_contract
                    AND a.quote=e.reference_quote
                    AND a.horizon_minutes=e.horizon_minutes
                    AND a.reference_opened_at=e.reference_opened_at
                    AND a.target_opened_at=e.target_opened_at
                    AND a.model_version=?
                   WHERE e.settled_at IS NULL
                     AND a.settled_at IS NOT NULL
                     AND a.settled_at<=? AND a.settled_at>e.issued_at
                   ORDER BY e.target_opened_at LIMIT 240""",
                (SOURCE_MODEL, stamp.isoformat()),
            ).fetchall()
        settled = 0
        for row in due:
            label = row["outcome_up"]
            if label not in (0, 1):
                continue
            challenger = float(row["probability_up"])
            incumbent = float(row["reference_probability_up"])
            with self.store.connect() as conn:
                result = conn.execute(
                    """UPDATE perp_multi_forward SET
                         settled_at=?,outcome_up=?,challenger_brier=?,
                         incumbent_brier=?
                       WHERE asset=? AND horizon_minutes=?
                         AND target_opened_at=? AND settled_at IS NULL""",
                    (stamp.isoformat(), label, (challenger-label)**2,
                     (incumbent-label)**2, row["asset"], row["horizon_minutes"],
                     row["target_opened_at"]),
                )
                settled += int(result.rowcount == 1)
        return settled

    def observe(
        self, quote: BookQuote | PerpReferenceBook, *, now: datetime | None = None,
    ) -> dict[str, int]:
        """Observe genuinely later same-market books, never inferred fills."""
        stamp = utc(now or datetime.now(UTC))
        quote.validate(now=stamp)
        venue = (
            "hyperliquid" if isinstance(quote, PerpReferenceBook)
            else "binance" if isinstance(quote, BookQuote)
            and quote.venue == "binance_usdm" else None
        )
        if venue is None:
            return {"opened": 0, "closed": 0}
        self.initialize()
        with self.store.connect() as conn:
            open_quotes = conn.execute(
                """SELECT q.*,e.probability_up,e.reference_probability_up
                   FROM perp_multi_quote_shadow AS q
                   JOIN perp_multi_forward AS e
                     ON e.asset=q.asset AND e.horizon_minutes=q.horizon_minutes
                    AND e.target_opened_at=q.target_opened_at
                   WHERE q.asset=? AND q.venue=? AND q.state='OPEN'
                     AND q.target_opened_at<=? AND q.target_opened_at>=?
                   ORDER BY q.target_opened_at LIMIT 12""",
                (quote.asset, venue,
                 (quote.exchange_at-timedelta(minutes=5)).isoformat(),
                 (quote.exchange_at-timedelta(minutes=5)-EXIT_GRACE).isoformat()),
            ).fetchall()
            closed = 0
            for row in open_quotes:
                if _time(row["entry_at"]) >= quote.exchange_at:
                    continue
                if (NOTIONAL_USDT / quote.bid >
                    min(quote.bid_quantity, quote.ask_quantity)):
                    continue

                def gross(prob: float, entry=row) -> float:
                    if max(prob, 1-prob) < 0.56:
                        return 0.0  # No position, no invented trading costs.
                    return (
                        (quote.bid / float(entry["entry_ask"]) - 1)*10000
                        if prob > 0.5 else
                        ((float(entry["entry_bid"]) - quote.ask) /
                         float(entry["entry_bid"]))*10000
                    )

                change = conn.execute(
                    """UPDATE perp_multi_quote_shadow
                       SET state='EXIT_QUOTED',exit_at=?,exit_bid=?,exit_ask=?,
                           challenger_gross_bps=?,incumbent_gross_bps=?
                       WHERE asset=? AND horizon_minutes=? AND target_opened_at=?
                         AND state='OPEN'""",
                    (quote.exchange_at.isoformat(),quote.bid,quote.ask,
                     gross(float(row["probability_up"])),
                     gross(float(row["reference_probability_up"])),
                     quote.asset,row["horizon_minutes"],row["target_opened_at"]),
                )
                closed += int(change.rowcount == 1)
            predictions = conn.execute(
                """SELECT * FROM perp_multi_forward
                   WHERE asset=? AND reference_venue=? AND issued_at<=?
                     AND issued_at>=? AND target_opened_at>=?
                   ORDER BY issued_at DESC LIMIT 16""",
                (quote.asset, venue, quote.exchange_at.isoformat(),
                 (quote.exchange_at-ENTRY_GRACE).isoformat(),
                 (quote.exchange_at-timedelta(minutes=4)).isoformat()),
            ).fetchall()
            opened = 0
            for row in predictions:
                if not (
                    _can_reference(venue, quote.asset,
                                   str(row["reference_contract"]),
                                   str(row["reference_quote"]))
                    and _time(row["issued_at"]) < quote.exchange_at
                    and quote.exchange_at <
                    _time(row["target_opened_at"])+timedelta(minutes=5)
                ):
                    continue
                if (NOTIONAL_USDT / quote.bid >
                    min(quote.bid_quantity,quote.ask_quantity)):
                    continue
                res = conn.execute(
                    """INSERT INTO perp_multi_quote_shadow (
                       asset,horizon_minutes,target_opened_at,venue,
                       entry_at,entry_bid,entry_ask,state)
                       VALUES (?,?,?,?,?,?,?,'OPEN')
                       ON CONFLICT DO NOTHING""",
                    (quote.asset,row["horizon_minutes"],
                     row["target_opened_at"],venue,
                     quote.exchange_at.isoformat(),quote.bid,quote.ask),
                )
                opened += int(res.rowcount == 1)
        return {"opened": opened, "closed": closed}

    def expire(self, *, now: datetime | None = None) -> int:
        stamp = utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            return conn.execute(
                """UPDATE perp_multi_quote_shadow SET state='MISSED_EXIT'
                   WHERE state='OPEN' AND target_opened_at<?""",
                ((stamp-timedelta(minutes=5)-EXIT_GRACE).isoformat(),),
            ).rowcount

    def report(self, *, asset: str, horizon_minutes: int,
               now: datetime | None = None) -> dict:
        if asset not in ASSETS or horizon_minutes not in HORIZONS:
            raise ValueError("unknown multi-venue market")
        stamp = utc(now or datetime.now(UTC))
        start = (stamp-timedelta(days=30)).isoformat()
        self.initialize()
        with self.store.connect() as conn:
            aggregate = conn.execute(
                """SELECT COUNT(*) AS issued,
                          SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END)
                              AS settled,
                          SUM(CASE WHEN settled_at IS NOT NULL
                              THEN challenger_brier ELSE 0 END) AS challenger_loss,
                          SUM(CASE WHEN settled_at IS NOT NULL
                              THEN incumbent_brier ELSE 0 END) AS incumbent_loss,
                          COUNT(DISTINCT CASE WHEN settled_at IS NOT NULL
                              THEN substr(target_opened_at,1,10) END) AS days,
                          MAX(contributor_count) AS max_sources,
                          MIN(contributor_count) AS min_sources
                   FROM perp_multi_forward
                   WHERE asset=? AND horizon_minutes=? AND issued_at>=?""",
                (asset,horizon_minutes,start),
            ).fetchone()
            quotes = conn.execute(
                """SELECT COUNT(*) AS n,
                    SUM(CASE WHEN q.state='EXIT_QUOTED' THEN 1 ELSE 0 END)
                        AS paired,
                    SUM(CASE WHEN q.state='MISSED_EXIT' THEN 1 ELSE 0 END)
                        AS missed,
                    SUM(CASE WHEN q.state='EXIT_QUOTED'
                             THEN q.challenger_gross_bps ELSE 0 END)
                        AS challenger_sum,
                    SUM(CASE WHEN q.state='EXIT_QUOTED'
                             THEN q.incumbent_gross_bps ELSE 0 END)
                        AS incumbent_sum,
                    SUM(CASE WHEN q.state='EXIT_QUOTED'
                        AND (e.probability_up>=0.56 OR
                             e.probability_up<=0.44)
                        THEN 1 ELSE 0 END) AS challenger_trades,
                    SUM(CASE WHEN q.state='EXIT_QUOTED'
                        AND (e.reference_probability_up>=0.56 OR
                             e.reference_probability_up<=0.44)
                        THEN 1 ELSE 0 END) AS incumbent_trades
                   FROM perp_multi_quote_shadow AS q
                   JOIN perp_multi_forward AS e
                     ON e.asset=q.asset AND e.horizon_minutes=q.horizon_minutes
                    AND e.target_opened_at=q.target_opened_at
                   WHERE e.asset=? AND e.horizon_minutes=? AND e.issued_at>=?
                     AND e.settled_at IS NOT NULL""",
                (asset,horizon_minutes,start),
            ).fetchone()
            daily_rows = conn.execute(
                """SELECT substr(e.target_opened_at,1,10) AS day,
                          COUNT(*) AS n,
                          SUM(q.challenger_gross_bps) AS challenger_sum,
                          SUM(q.incumbent_gross_bps) AS incumbent_sum,
                          SUM(CASE WHEN e.probability_up>=0.56
                                    OR e.probability_up<=0.44 THEN 1 ELSE 0 END)
                             AS challenger_trades,
                          SUM(CASE WHEN e.reference_probability_up>=0.56
                                    OR e.reference_probability_up<=0.44
                                   THEN 1 ELSE 0 END) AS incumbent_trades
                   FROM perp_multi_quote_shadow AS q
                   JOIN perp_multi_forward AS e
                     ON e.asset=q.asset AND e.horizon_minutes=q.horizon_minutes
                    AND e.target_opened_at=q.target_opened_at
                   WHERE e.asset=? AND e.horizon_minutes=? AND e.issued_at>=?
                     AND e.settled_at IS NOT NULL
                     AND q.state='EXIT_QUOTED'
                   GROUP BY substr(e.target_opened_at,1,10)
                   ORDER BY day""",
                (asset,horizon_minutes,start),
            ).fetchall()
        n = int(aggregate["settled"] or 0)
        paired = int(quotes["paired"] or 0)
        ml = float(aggregate["challenger_loss"] or 0)/n if n else None
        bl = float(aggregate["incumbent_loss"] or 0)/n if n else None
        cost_results = {}
        daily_stats = {}
        for label,bps in COST_SCENARIOS:
            challenger_days = [
                (float(x["challenger_sum"]) - bps*int(x["challenger_trades"]))
                / int(x["n"])
                for x in daily_rows
            ]
            incumbent_days = [
                (float(x["incumbent_sum"]) - bps*int(x["incumbent_trades"]))
                / int(x["n"])
                for x in daily_rows
            ]
            diff_days = [x-y for x,y in zip(challenger_days,incumbent_days)]
            count_days = len(challenger_days)

            def lower(days: list[float]) -> float | None:
                if len(days) < 2:
                    return None
                mean=sum(days)/len(days)
                variance=sum((x-mean)**2 for x in days)/(len(days)-1)
                return mean-2.58*math.sqrt(variance/len(days))

            daily_stats[label] = {
                "days":count_days,
                "challenger_lower":lower(challenger_days),
                "incremental_lower":lower(diff_days),
                "positive_days":sum(x>0 for x in challenger_days),
            }
            cost_results[label] = {
                "challenger_mean_net_bps": (
                    (float(quotes["challenger_sum"] or 0) -
                     int(quotes["challenger_trades"] or 0)*bps)/paired
                    if paired else None
                ),
                "incumbent_mean_net_bps": (
                    (float(quotes["incumbent_sum"] or 0) -
                     int(quotes["incumbent_trades"] or 0)*bps)/paired
                    if paired else None
                ),
            }
        coverage = paired/n if n else 0.0
        severe = cost_results.get("severe", {})
        severe_daily = daily_stats.get("severe", {})
        incumbent_outperformed = (
            n >= MIN_PAIRED_WINDOWS and
            int(aggregate["days"] or 0) >= MIN_DAYS and
            ml is not None and bl is not None and
            bl-ml >= 0.005
        )
        cost_advantage = (
            paired >= MIN_PAIRED_WINDOWS and coverage >= 0.80
            and severe_daily.get("days", 0) >= MIN_DAYS
            and severe.get("challenger_mean_net_bps") is not None
            and severe["challenger_mean_net_bps"] > 0
            and severe.get("incumbent_mean_net_bps") is not None
            and severe["challenger_mean_net_bps"] >
                severe["incumbent_mean_net_bps"]
            and severe_daily.get("challenger_lower") is not None
            and severe_daily["challenger_lower"] > 0
            and severe_daily.get("incremental_lower") is not None
            and severe_daily["incremental_lower"] > 0
            and severe_daily["positive_days"] >= math.ceil(
                severe_daily["days"]*0.8
            )
        )
        shadow_candidate_qualified = incumbent_outperformed and cost_advantage
        return {
            "asset":asset,"horizon_minutes":horizon_minutes,
            "issued":int(aggregate["issued"] or 0),
            "settled":n,"days":int(aggregate["days"] or 0),
            "max_sources":int(aggregate["max_sources"] or 0),
            "min_sources":int(aggregate["min_sources"] or 0),
            "challenger_brier":ml,"incumbent_brier":bl,
            "brier_improvement":bl-ml if n else None,
            "quote_pairs":paired,"quote_missed":int(quotes["missed"] or 0),
            "quote_entries":int(quotes["n"] or 0),
            "quote_coverage": coverage,
            "after_cost":cost_results,
            "daily_stress": daily_stats,
            "model_advantage_qualified":incumbent_outperformed,
            "cost_advantage_qualified":cost_advantage,
            "shadow_candidate_qualified":shadow_candidate_qualified,
            "status": ("SHADOW_CANDIDATE_PASSED_NOT_DEPLOYED"
                       if shadow_candidate_qualified
                       else "RESEARCH_ONLY_NOT_APPROVED"),
            "replacement_authorized":False,
        }
