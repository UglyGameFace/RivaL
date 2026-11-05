"""RivaL Intelligence: reviews what RivaL's prediction models predict.

The prediction models make every call. RivaL Intelligence (the private LFM3
model) reads those calls, how many exchanges agree, each model's graded
record and the market evidence, then decides per timeframe whether to
"follow" the models' call or "abstain", and explains why.

It never invents a direction: a "follow" takes the models' consensus
direction, and only when the consensus is clear. Every stance is stored
against the exact model forecasts of that 5-minute slot, so the bot can
grade whether following RivaL Intelligence beats following the raw model.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor

_LOG = logging.getLogger(__name__)
HORIZONS = (5, 15, 60)
# A "follow" needs a clear model consensus: mean probability at least this
# far from 50% and most exchanges on the same side.
CONSENSUS_MARGIN = 0.02
CONSENSUS_SHARE = 2 / 3
MAX_CHALLENGERS = 3
# A record ranks a challenger only once it has this many graded outcomes (the
# same bar as the regression watchdog). Below it, a few lucky results could
# put a challenger ahead of one with hundreds of solid ones.
MIN_GRADED_TO_RANK = 50
RECORD_TTL = timedelta(minutes=30)
_RECORDS: dict[str, tuple[datetime, dict]] = {}


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.utcoffset() is not None else value.replace(tzinfo=UTC)


def model_calls(market, *, asset: str, now: datetime | None = None,
                summary: dict | None = None) -> dict:
    """Latest-slot calls of every live and challenger model, with records."""
    stamp = utc(now or datetime.now(UTC))
    since = (stamp - timedelta(minutes=12)).isoformat()
    with market.store.connect() as conn:
        rows = conn.execute(
            """SELECT horizon_minutes, target_opened_at, probability_up
               FROM perp_forward_audit
               WHERE asset=? AND model_version=? AND issued_at>=? AND issued_at<=?""",
            (asset, LIVE_MODEL, since, stamp.isoformat()),
        ).fetchall()
        try:
            challengers = conn.execute(
                """SELECT horizon_minutes, family, target_opened_at, probability_up
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND (family LIKE 'pooled_%' OR family LIKE 'flow_%'
                                      OR family LIKE 'calib_%')
                     AND issued_at>=? AND issued_at<=?""",
                (asset, since, stamp.isoformat()),
            ).fetchall()
        except Exception:  # noqa: BLE001 - optional challenger table
            challengers = []
    if summary is None:
        try:
            summary = ForwardAccuracyMonitor(market).summary(asset=asset, now=stamp)
        except Exception:  # noqa: BLE001 - calls are still useful without a record
            _LOG.warning("RivaL Intelligence model record unavailable")
            summary = {}
    out: dict[str, dict] = {}
    for horizon in HORIZONS:
        entry: dict[str, dict] = {}
        live = _latest_slot([r for r in rows if int(r["horizon_minutes"]) == horizon])
        if live is not None:
            part = (summary.get("last_7d") or {}).get(horizon) or {}
            entry["live"] = {**live, "record": {
                "hit_rate": part.get("hit_rate"), "graded": int(part.get("scored") or 0),
                "beats_basic": part.get("model_beats_baseline") is True,
            }}
        families = {str(r["family"]) for r in challengers if int(r["horizon_minutes"]) == horizon}
        records = challenger_records(market, asset=asset, now=stamp,
                                     families={(horizon, f) for f in families})
        ranked = []
        for family in families:
            call = _latest_slot([r for r in challengers if int(r["horizon_minutes"]) == horizon
                                 and str(r["family"]) == family])
            if call is not None:
                record = records.get((horizon, family), {})
                ranked.append((_rank_key(record), family, call, record))
        # The reviewer reads at most 12 lines: live + the 3 best-graded
        # challengers per timeframe, each with its real 7-day record.
        for _, family, call, record in sorted(ranked)[:MAX_CHALLENGERS]:
            entry[_short(family)] = {**call, "record": {
                k: record[k] for k in ("hit_rate", "graded", "beats_basic") if k in record}}
        if entry:
            out[str(horizon)] = entry
    return out


def _rank_key(record: dict) -> tuple:
    """Sort key, best first: enough graded results ranked by Brier gain over
    the up-rate; then the rest by how many results they have."""
    graded = int(record.get("graded") or 0)
    gain = record.get("brier_gain")
    if graded >= MIN_GRADED_TO_RANK and gain is not None:
        return (0, -float(gain))
    return (1, -graded)


def challenger_records(market, *, asset: str, now: datetime,
                       families: set[tuple[int, str]]) -> dict:
    """7-day graded record of each challenger: hit rate and Brier vs up-rate.

    Only outcomes settled before ``now``. Cached per asset for RECORD_TTL.
    """
    if not families:
        return {}
    hit = _RECORDS.get(asset)
    if hit is not None and timedelta(0) <= now - hit[0] < RECORD_TTL and \
            all(key in hit[1] or key in hit[1].get("_none", ()) for key in families):
        return hit[1]
    names = sorted({f for _, f in families})
    marks = ",".join("?" * len(names))
    out: dict = {"_none": set()}
    try:
        with market.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT horizon_minutes AS h, family, COUNT(*) AS n,
                          AVG(model_brier) AS m, AVG(baseline_brier) AS b,
                          SUM(CASE WHEN (probability_up>0.5 AND outcome_up=1)
                                    OR (probability_up<0.5 AND outcome_up=0)
                                   THEN 1 ELSE 0 END) AS hits,
                          SUM(CASE WHEN probability_up<>0.5 THEN 1 ELSE 0 END) AS decided
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND family IN ({marks}) AND issued_at>=?
                     AND settled_at IS NOT NULL AND settled_at<=?
                     AND outcome_up IN (0,1) AND model_brier IS NOT NULL
                     AND baseline_brier IS NOT NULL
                   GROUP BY horizon_minutes, family""",
                (asset, *names, (now - timedelta(days=7)).isoformat(), now.isoformat()),
            ).fetchall()
    except Exception:  # noqa: BLE001 - a review is still useful without records
        _LOG.warning("RivaL Intelligence challenger records unavailable")
        return {}
    for r in rows:
        decided = int(r["decided"] or 0)
        out[(int(r["h"]), str(r["family"]))] = {
            "graded": int(r["n"]),
            "hit_rate": round(int(r["hits"] or 0) / decided, 4) if decided else None,
            "beats_basic": float(r["m"]) < float(r["b"]),
            "brier_gain": round(float(r["b"]) - float(r["m"]), 6),
        }
    out["_none"] = {key for key in families if key not in out}
    _RECORDS[asset] = (now, out)
    return out


def market_context(market, *, asset: str, now: datetime | None = None) -> dict:
    """Positioning numbers from RivaL's own feeds when Binance is unavailable.

    Same keys as BinanceContext.numbers: Hyperliquid funding (as an 8-hour
    rate), perp premium over the oracle and 1-hour open-interest change;
    taker-buy share of the last hour of Hyperliquid + OKX perp trades. Only
    rows recorded at or before ``now``; missing data is left out, not guessed.
    """
    stamp = utc(now or datetime.now(UTC))
    out: dict = {}
    try:
        with market.store.connect() as conn:
            ctx = conn.execute(
                """SELECT observed_at, mark_price, oracle_price, funding_rate,
                          open_interest_base FROM perp_market_contexts
                   WHERE venue='hyperliquid' AND contract=? AND observed_at<=?
                     AND observed_at>=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (asset, stamp.isoformat(), (stamp - timedelta(minutes=15)).isoformat()),
            ).fetchone()
            if ctx is not None:
                if float(ctx["oracle_price"] or 0) > 0:
                    out["premium_bps"] = round(
                        (float(ctx["mark_price"]) / float(ctx["oracle_price"]) - 1) * 1e4, 2)
                out["funding_bps"] = round(float(ctx["funding_rate"]) * 8 * 1e4, 3)
                before = conn.execute(
                    """SELECT open_interest_base FROM perp_market_contexts
                       WHERE venue='hyperliquid' AND contract=? AND observed_at<=?
                         AND observed_at>=?
                       ORDER BY observed_at DESC LIMIT 1""",
                    (asset, (stamp - timedelta(minutes=55)).isoformat(),
                     (stamp - timedelta(minutes=75)).isoformat()),
                ).fetchone()
                old, new = (float(before["open_interest_base"] or 0) if before else 0,
                            float(ctx["open_interest_base"] or 0))
                if old > 0 and new > 0:
                    out["open_interest_change_1h"] = round(new / old - 1, 5)
            flow = conn.execute(
                """SELECT SUM(buy_volume) AS buy, SUM(sell_volume) AS sell
                   FROM perp_liquidity_5m
                   WHERE asset=? AND venue IN ('hyperliquid','okx') AND complete=1
                     AND bucket_start>=? AND recorded_at<=?""",
                (asset, (stamp - timedelta(hours=1)).isoformat(), stamp.isoformat()),
            ).fetchone()
            total = float(flow["buy"] or 0) + float(flow["sell"] or 0) if flow else 0
            if total > 0:
                out["taker_buy_share_1h"] = round(float(flow["buy"]) / total, 4)
    except Exception:  # noqa: BLE001 - context is optional evidence
        _LOG.warning("RivaL Intelligence market context unavailable")
    return out


def _short(family: str) -> str:
    return "".join(ch for ch in family.lower() if ch.isalnum() or ch == "_")[:32]


def _latest_slot(rows) -> dict | None:
    if not rows:
        return None
    slot = max(str(r["target_opened_at"]) for r in rows)
    probs = [float(r["probability_up"]) for r in rows if str(r["target_opened_at"]) == slot]
    probs = [p for p in probs if math.isfinite(p) and 0 <= p <= 1]
    if not probs:
        return None
    return {"slot": slot, "p_up": round(sum(probs) / len(probs), 4),
            "venues": len(probs), "venues_up": sum(p > 0.5 for p in probs)}


def consensus_direction(call: dict | None) -> str | None:
    """'up'/'down' only when the models clearly agree; otherwise None."""
    if not call or not call.get("venues"):
        return None
    p, venues, up = float(call["p_up"]), int(call["venues"]), int(call["venues_up"])
    if p >= 0.5 + CONSENSUS_MARGIN and up >= CONSENSUS_SHARE * venues:
        return "up"
    if p <= 0.5 - CONSENSUS_MARGIN and (venues - up) >= CONSENSUS_SHARE * venues:
        return "down"
    return None


class IntelligenceLedger:
    """Stores every RivaL Intelligence review and grades it later."""

    def __init__(self, store) -> None:
        self.store = store
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_intelligence_calls (
                    asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
                    target_opened_at TEXT NOT NULL, issued_at TEXT NOT NULL,
                    model TEXT NOT NULL, stance TEXT NOT NULL,
                    direction TEXT NOT NULL, consensus_up DOUBLE PRECISION NOT NULL,
                    venues INTEGER NOT NULL, venues_up INTEGER NOT NULL,
                    thesis TEXT NOT NULL, risk TEXT NOT NULL,
                    PRIMARY KEY (asset, horizon_minutes, target_opened_at)
                )"""
            )
        self._ready = True

    def record(self, *, asset: str, calls: dict, review: dict, model: str,
               issued_at: datetime) -> int:
        """Persist one stance per timeframe that had a live model call."""
        self.initialize()
        saved = 0
        with self.store.connect() as conn:
            for horizon in HORIZONS:
                live = (calls.get(str(horizon)) or {}).get("live")
                stance = review.get(f"stance_{horizon}m")
                if live is None or stance not in ("follow", "abstain"):
                    continue
                # Too late to count: the slot's result window already opened.
                if utc(issued_at) >= utc(datetime.fromisoformat(live["slot"])) + timedelta(minutes=5):
                    continue
                consensus = consensus_direction(live)
                direction = consensus if stance == "follow" and consensus else "none"
                row = conn.execute(
                    """INSERT INTO rival_intelligence_calls
                       (asset,horizon_minutes,target_opened_at,issued_at,model,stance,
                        direction,consensus_up,venues,venues_up,thesis,risk)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, horizon, live["slot"], utc(issued_at).isoformat(), model,
                     stance, direction, float(live["p_up"]), int(live["venues"]),
                     int(live["venues_up"]), str(review.get("thesis", ""))[:240],
                     str(review.get("risk", ""))[:160]),
                )
                saved += int(row.rowcount == 1)
        return saved

    def latest(self, *, asset: str) -> dict | None:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_intelligence_calls WHERE asset=?
                   ORDER BY issued_at DESC LIMIT 3""", (asset,),
            ).fetchall()
        if not rows:
            return None
        return {"issued_at": str(rows[0]["issued_at"]), "thesis": str(rows[0]["thesis"]),
                "risk": str(rows[0]["risk"]),
                "stances": {int(r["horizon_minutes"]): (str(r["stance"]), str(r["direction"]))
                            for r in rows}}

    def report(self, *, asset: str, horizon: int, now: datetime | None = None) -> dict:
        """Grade 'follow' and 'abstain' the same way, on what was known then.

        Each review is graded only against the forecasts that existed when it
        was made (issued at or before the review) and only with results that
        were settled by ``now``, so a later forecast or a later settlement can
        never rewrite an old review's score. Both stances use one rule: the
        model's lean at review time (the stored consensus) against the
        majority outcome of those same exchanges. "Abstained" therefore shows
        how often following the model anyway would have been right; exact
        50/50 leans and tied outcomes are left out of both.
        """
        self.initialize()
        stamp = utc(now or datetime.now(UTC))
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT c.stance, c.direction, c.target_opened_at, c.consensus_up,
                          AVG(f.outcome_up) AS up_rate, COUNT(*) AS n
                   FROM rival_intelligence_calls c
                   JOIN perp_forward_audit f
                     ON f.asset=c.asset AND f.horizon_minutes=c.horizon_minutes
                    AND f.target_opened_at=c.target_opened_at AND f.model_version=?
                    AND f.outcome_up IN (0,1) AND f.issued_at<=c.issued_at
                    AND f.settled_at IS NOT NULL AND f.settled_at<=?
                   WHERE c.asset=? AND c.horizon_minutes=? AND c.issued_at>=?
                     AND c.issued_at<=?
                   GROUP BY c.stance, c.direction, c.target_opened_at, c.consensus_up""",
                (LIVE_MODEL, stamp.isoformat(), asset, horizon,
                 (stamp - timedelta(days=7)).isoformat(), stamp.isoformat()),
            ).fetchall()

        def outcome(r) -> str | None:
            up_rate = float(r["up_rate"])
            return "up" if up_rate > 0.5 else "down" if up_rate < 0.5 else None

        def lean(r) -> str | None:
            if r["stance"] == "follow":
                return str(r["direction"]) if r["direction"] in ("up", "down") else None
            p = float(r["consensus_up"])
            return "up" if p > 0.5 else "down" if p < 0.5 else None

        graded = [(r, lean(r), outcome(r)) for r in rows]
        graded = [(r, call, result) for r, call, result in graded
                  if call is not None and result is not None]
        followed = [call == result for r, call, result in graded if r["stance"] == "follow"]
        abstained = [call == result for r, call, result in graded if r["stance"] == "abstain"]
        every = [call == result for _, call, result in graded]
        return {
            "followed": len(followed),
            "followed_hit_rate": sum(followed) / len(followed) if followed else None,
            "abstained": len(abstained),
            "model_hit_when_abstained": (sum(abstained) / len(abstained)
                                         if abstained else None),
            "model_hit_overall": sum(every) / len(every) if every else None,
            "slots_graded": len(every),
        }
