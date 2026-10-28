"""Immutable, truly-forward, read-only accuracy audit for RivaL.

A forecast must exist in PostgreSQL before the target bar was observed.
Never retroactively turn backfilled candles into prospective predictions.
Uses only model weights trained on outcomes already known at issuance.
"""
from __future__ import annotations

import itertools
import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import LOOKBACK, live_examples, persisted_candles
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import walk_forward
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor

_LOG = logging.getLogger(__name__)
MODEL = "prospective-logistic-shadow-v1"
HORIZONS = (5, 15)
MIN_PREVIOUS_EVALUATED = 20
MAX_STREAMS_PER_CYCLE = 10
MAX_PENDING_SETTLEMENTS = 120


def _features(candles) -> tuple[float, ...]:
    """Match the causal feature vector in live_examples() for the last bar."""
    window = tuple(candles[-LOOKBACK - 1:])
    if len(window) != LOOKBACK + 1:
        raise ValueError("not enough point-in-time candles")
    for left, right in itertools.pairwise(window):
        if (
            (utc(right.opened_at) - utc(left.opened_at)).total_seconds() != 300
            or (left.venue, left.contract, left.quote) !=
            (right.venue, right.contract, right.quote)
        ):
            raise ValueError("latest feature window is not continuous")
    closes = [x.close_price for x in window]
    if any(not math.isfinite(x) or x <= 0 for x in closes):
        raise ValueError("invalid prospective prices")
    returns = [math.log(b / a) for a, b in itertools.pairwise(closes)]
    avg = sum(returns) / len(returns)
    variance = sum((v - avg) ** 2 for v in returns) / len(returns)
    current = window[-1]
    volume_avg = sum(x.volume_base for x in window[:-1]) / LOOKBACK
    return (
        max(-5.0, min(5.0, returns[-1] * 100)),
        max(-5.0, min(5.0, math.log(closes[-1] / closes[0]) * 100)),
        max(0.0, min(5.0, math.sqrt(variance) * 100)),
        max(0.0, min(5.0, (current.high_price - current.low_price)
                         / current.close_price * 100)),
        max(-2.0, min(2.0, math.log((current.volume_base + 1)
                                   / (volume_avg + 1)))),
    )


def _probability(weights: tuple[float, ...], features: tuple[float, ...]) -> float:
    if len(weights) != len(features) + 1:
        raise ValueError("invalid trained shadow model")
    value = max(-30.0, min(30.0, sum(
        w * x for w, x in zip(weights, (1.0, *features))
    )))
    return 1.0 / (1.0 + math.exp(-value))


class ForwardAccuracyMonitor:
    """Issue, settle and score immutable prospective research only."""

    def __init__(self, market: PerpetualMarketStore) -> None:
        self.market = market
        self.store = market.store

    def initialize(self) -> None:
        self.market.initialize()
        ShadowLearningMonitor(self.store).initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_forward_audit (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    reference_opened_at TEXT NOT NULL,
                    reference_close DOUBLE PRECISION NOT NULL,
                    issued_at TEXT NOT NULL,
                    target_opened_at TEXT NOT NULL,
                    probability_up DOUBLE PRECISION NOT NULL,
                    baseline_up DOUBLE PRECISION NOT NULL,
                    training_evaluated INTEGER NOT NULL,
                    settled_at TEXT,
                    outcome_up INTEGER,
                    realized_close DOUBLE PRECISION,
                    model_brier DOUBLE PRECISION,
                    baseline_brier DOUBLE PRECISION,
                    PRIMARY KEY (
                        venue, contract, quote, horizon_minutes,
                        model_version, reference_opened_at
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_perp_forward_pending
                    ON perp_forward_audit (settled_at,target_opened_at);
                CREATE INDEX IF NOT EXISTS idx_perp_forward_asset_horizon
                    ON perp_forward_audit (asset,horizon_minutes,issued_at);
                """
            )

    def _settle(self, now: datetime) -> int:
        with self.store.connect() as conn:
            pending = conn.execute(
                """SELECT venue,contract,quote,asset,horizon_minutes,
                          model_version,reference_opened_at,reference_close,
                          issued_at,target_opened_at,probability_up,baseline_up
                   FROM perp_forward_audit
                   WHERE settled_at IS NULL AND target_opened_at<?
                     AND target_opened_at>=?
                   ORDER BY target_opened_at DESC LIMIT ?""",
                ((now - timedelta(minutes=5)).isoformat(),
                 (now - timedelta(days=2)).isoformat(),
                 MAX_PENDING_SETTLEMENTS),
            ).fetchall()
        resolved = 0
        for pred in pending:
            try:
                with self.store.connect() as conn:
                    row = conn.execute(
                        """SELECT close_price,first_observed_at,closed_at
                           FROM perp_market_candles WHERE venue=? AND contract=?
                             AND quote=? AND interval_minutes=5 AND opened_at=?""",
                        (pred["venue"], pred["contract"], pred["quote"],
                         pred["target_opened_at"]),
                    ).fetchone()
                if row is None:
                    continue
                observed = utc(datetime.fromisoformat(str(row["first_observed_at"])))
                issued = utc(datetime.fromisoformat(str(pred["issued_at"])))
                close = utc(datetime.fromisoformat(str(row["closed_at"])))
                if observed <= issued or observed > now or close >= now:
                    continue  # A backfill known at issuance is not a forward outcome.
                value = float(row["close_price"])
                previous = float(pred["reference_close"])
                if not math.isfinite(value) or value <= 0:
                    continue  # Flat moves remain unresolved, never forced to up/down.
                label = int(value > previous)
                p = float(pred["probability_up"])
                b = float(pred["baseline_up"])
                with self.store.connect() as conn:
                    result = conn.execute(
                        """UPDATE perp_forward_audit
                           SET settled_at=?, outcome_up=?, realized_close=?,
                               model_brier=?,baseline_brier=?
                           WHERE venue=? AND contract=? AND quote=?
                             AND horizon_minutes=? AND model_version=?
                             AND reference_opened_at=? AND settled_at IS NULL""",
                        (
                            now.isoformat(), label, value, (p - label) ** 2,
                            (b - label) ** 2, pred["venue"], pred["contract"],
                            pred["quote"], pred["horizon_minutes"],
                            pred["model_version"], pred["reference_opened_at"],
                        ),
                    )
                    resolved += int(result.rowcount == 1)
            except (ValueError, TypeError, ArithmeticError):
                _LOG.warning("Prospective settlement skipped corrupt market row")
        return resolved

    def run_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        stamp = utc(now or datetime.now(UTC))
        if stamp > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("prospective forecast time cannot be in the future")
        self.initialize()
        settled = self._settle(stamp)
        issued = 0
        # Reuse the same bounded, freshness-checked stream selection as
        # existing offline evaluation, without external HTTP requests.
        eligible = ShadowLearningMonitor(self.store)._eligible_streams(
            now=stamp, limit=30,
        )
        # Rotate across source groups on UTC five-minute boundaries. This
        # guarantees that a still-untrained market cannot continually block
        # the other markets, even when the bot restarts.
        groups = max(
            1, (len(eligible) + MAX_STREAMS_PER_CYCLE - 1)
            // MAX_STREAMS_PER_CYCLE,
        )
        slot = int(stamp.timestamp() // 300) % groups
        start = slot * MAX_STREAMS_PER_CYCLE
        selection = eligible[start:start + MAX_STREAMS_PER_CYCLE]
        for venue, contract, quote in selection:
            try:
                rows = self.market.recent_candles(
                    venue=venue, contract=contract, quote=quote,
                    interval_minutes=5, count=1000, as_of=stamp,
                )
                if len(rows) < 65:
                    continue
                candles = persisted_candles(rows)
                last = candles[-1]
                if not timedelta(0) <= stamp - utc(last.observed_at) <= timedelta(
                    minutes=10
                ):
                    continue
                features = _features(candles)
                for horizon in HORIZONS:
                    samples = live_examples(candles, horizon_minutes=horizon)
                    if len(samples) < MIN_PREVIOUS_EVALUATED + 20:
                        continue
                    tested = walk_forward(
                        samples, as_of=stamp, minimum_evaluated=50,
                    )
                    if tested.evaluated < MIN_PREVIOUS_EVALUATED:
                        continue
                    usable = [
                        sample for sample in samples
                        if utc(sample.label_known_at) <= stamp
                    ]
                    positive = sum(x.label_up for x in usable)
                    baseline = (positive + 5) / (len(usable) + 10)
                    probability = _probability(tested.final_weights, features)
                    asset = ("BTC" if contract.startswith(("BTC", "XBT"))
                             else "ETH" if contract.startswith("ETH")
                             else "SOL" if contract.startswith("SOL") else None)
                    if asset is None:
                        continue
                    target_open = utc(last.opened_at) + timedelta(minutes=horizon)
                    # The forecast is written only while the target close is
                    # still genuinely in the future. A late job cannot
                    # retrospectively issue a 'prediction'.
                    if stamp >= target_open + timedelta(minutes=5, milliseconds=-1):
                        continue
                    with self.store.connect() as conn:
                        record = conn.execute(
                            """INSERT INTO perp_forward_audit (
                                 venue,contract,quote,asset,horizon_minutes,
                                 model_version,reference_opened_at,reference_close,
                                 issued_at,target_opened_at,probability_up,
                                 baseline_up,training_evaluated
                               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT DO NOTHING""",
                            (
                                venue, contract, quote, asset, horizon, MODEL,
                                utc(last.opened_at).isoformat(), last.close_price,
                                stamp.isoformat(), target_open.isoformat(),
                                probability, baseline, tested.evaluated,
                            ),
                        )
                        issued += int(record.rowcount == 1)
            except (ValueError, TypeError, ArithmeticError):
                _LOG.warning(
                    "Prospective issue skipped invalid venue=%s contract=%s",
                    venue, contract,
                )
                continue
        _LOG.info(
            "RivaL prospective shadow audit issued=%s settled=%s",
            issued, settled,
        )
        return issued, settled

    def summary(self, *, asset: str, now: datetime | None = None) -> dict:
        if asset not in ("BTC", "ETH", "SOL"):
            raise ValueError("invalid asset")
        stamp = utc(now or datetime.now(UTC))
        start = (stamp - timedelta(days=7)).isoformat()
        prior = (stamp - timedelta(days=14)).isoformat()
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,horizon_minutes,issued_at,settled_at,
                          outcome_up,probability_up,baseline_up,model_brier,
                          baseline_brier
                   FROM perp_forward_audit
                   WHERE asset=? AND issued_at>=? AND issued_at<=?
                   ORDER BY issued_at""",
                (asset, prior, stamp.isoformat()),
            ).fetchall()
        def bucket(items) -> dict:
            settled_rows = [x for x in items if x["settled_at"] is not None]
            n = len(settled_rows)
            if not n:
                return {
                    "issued": len(items), "settled": 0, "hit_rate": None,
                    "brier": None, "baseline_brier": None,
                    "venues": 0, "model_beats_baseline": None,
                }
            brier = sum(float(r["model_brier"]) for r in settled_rows) / n
            base = sum(float(r["baseline_brier"]) for r in settled_rows) / n
            accuracy = sum(
                int((float(r["probability_up"]) > 0.5) == bool(r["outcome_up"]))
                for r in settled_rows if float(r["probability_up"]) != 0.5
            )
            decided = sum(float(r["probability_up"]) != 0.5 for r in settled_rows)
            return {
                "issued": len(items), "settled": n,
                "venues": len({r["venue"] for r in settled_rows}),
                "hit_rate": accuracy / decided if decided else None,
                "brier": brier, "baseline_brier": base,
                "model_beats_baseline": brier < base if n >= 50 else None,
            }
        recent = [r for r in rows if r["issued_at"] >= start]
        earlier = [r for r in rows if prior <= r["issued_at"] < start]
        by_horizon = {
            horizon: bucket([r for r in recent if r["horizon_minutes"] == horizon])
            for horizon in HORIZONS
        }
        return {
            "asset": asset, "as_of": stamp, "model": MODEL,
            "last_7d": by_horizon, "prior_7d": {
                horizon: bucket([r for r in earlier if r["horizon_minutes"] == horizon])
                for horizon in HORIZONS
            },
            "threshold": 50, "independent_trials": False,
        }
