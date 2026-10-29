"""Bounded, automatic shadow learning on verified historical observations.

Recomputes a leakage-gated online model using settled data already collected
by RivaL. Reports evidence; never promotes a trading signal or sends an order.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from parlay_bot.perpetuals.features import live_examples, persisted_candles
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import walk_forward
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

if TYPE_CHECKING:
    from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
MODEL = "shadow-online-logistic-v1"
HORIZONS = (5, 15)
MAX_STREAMS = 10
MAX_CANDLES = 1000
MIN_EVALUATED = 50


def _asset(contract: str) -> str | None:
    for asset in ("BTC", "ETH", "SOL"):
        if contract.startswith(asset):
            return asset
    if contract.startswith("XBT"):
        return "BTC"
    return None


class ShadowLearningMonitor:
    """Evaluate at most ten distinct venue/contracts per scheduled cycle."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.market = PerpetualMarketStore(store)
        self.store = store

    def initialize(self) -> None:
        self.market.initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_shadow_evaluations (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    observed_candles INTEGER NOT NULL,
                    causal_examples INTEGER NOT NULL,
                    evaluated INTEGER NOT NULL,
                    matured_updates INTEGER NOT NULL,
                    brier DOUBLE PRECISION,
                    baseline_brier DOUBLE PRECISION,
                    directional_accuracy DOUBLE PRECISION,
                    candidate_beats_baseline INTEGER NOT NULL,
                    PRIMARY KEY (venue,contract,quote,horizon_minutes,model_version)
                );
                CREATE INDEX IF NOT EXISTS idx_perp_shadow_asset
                    ON perp_shadow_evaluations (asset, evaluated_at);
                """
            )

    def _eligible_streams(
        self, *, now: datetime, limit: int = MAX_STREAMS,
    ) -> list[tuple[str, str, str]]:
        if not 1 <= limit <= 30:
            raise ValueError("invalid shadow stream discovery limit")
        with self.store.connect() as conn:
            observed = conn.execute(
                """SELECT venue,contract,quote,MAX(first_observed_at) AS last_seen
                   FROM perp_market_candles
                   WHERE interval_minutes=5
                   GROUP BY venue,contract,quote
                   ORDER BY venue,contract,quote LIMIT 80"""
            ).fetchall()
            prior = conn.execute(
                """SELECT venue,contract,quote,MAX(evaluated_at) AS last_eval
                   FROM perp_shadow_evaluations
                   WHERE model_version=?
                   GROUP BY venue,contract,quote""",
                (MODEL,),
            ).fetchall()
        evaluated = {
            (x["venue"], x["contract"], x["quote"]): x["last_eval"]
            for x in prior
        }
        valid = []
        for r in observed:
            stream = (str(r["venue"]), str(r["contract"]), str(r["quote"]))
            if _asset(stream[1]) is None:
                continue
            seen = utc(datetime.fromisoformat(str(r["last_seen"])))
            if not timedelta(0) <= now - seen <= timedelta(minutes=20):
                continue
            valid.append(stream)
        # Round robin across process restarts, weighted by last saved evaluation.
        return sorted(valid, key=lambda x: (evaluated.get(x) or "", x))[:limit]

    def run_once(self, *, now: datetime | None = None) -> int:
        """One bounded offline evaluation, without exchange requests or API keys."""
        stamp = utc(now or datetime.now(UTC))
        if stamp > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("evaluation time cannot be in the future")
        self.initialize()
        completed = 0
        for venue, contract, quote in self._eligible_streams(now=stamp):
            try:
                rows = self.market.recent_candles(
                    venue=venue, contract=contract, quote=quote,
                    interval_minutes=5, count=MAX_CANDLES, as_of=stamp,
                )
                candles = persisted_candles(rows)
                evaluations = [
                    (
                        horizon,
                        samples := live_examples(candles, horizon_minutes=horizon),
                        walk_forward(
                            samples, as_of=stamp, minimum_evaluated=MIN_EVALUATED
                        ),
                    )
                    for horizon in HORIZONS
                ]
            except (ValueError, TypeError, ArithmeticError) as exc:
                # Report the real validation failure without stopping other sources.
                _LOG.warning(
                    "RivaL shadow learning skipped invalid venue=%s contract=%s reason=%s: %s",
                    venue, contract, type(exc).__name__, str(exc)[:240],
                )
                continue
            for horizon, samples, result in evaluations:
                asset = _asset(contract)
                assert asset is not None
                with self.store.connect() as conn:
                    conn.execute(
                        """INSERT INTO perp_shadow_evaluations (
                            venue,contract,quote,asset,horizon_minutes,model_version,
                            evaluated_at,observed_candles,causal_examples,evaluated,
                            matured_updates,brier,baseline_brier,directional_accuracy,
                            candidate_beats_baseline
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT (venue,contract,quote,horizon_minutes,model_version)
                           DO UPDATE SET
                            evaluated_at=excluded.evaluated_at,
                            observed_candles=excluded.observed_candles,
                            causal_examples=excluded.causal_examples,
                            evaluated=excluded.evaluated,
                            matured_updates=excluded.matured_updates,
                            brier=excluded.brier,
                            baseline_brier=excluded.baseline_brier,
                            directional_accuracy=excluded.directional_accuracy,
                            candidate_beats_baseline=excluded.candidate_beats_baseline
                           WHERE perp_shadow_evaluations.evaluated_at <= excluded.evaluated_at""",
                        (
                            venue, contract, quote, asset, horizon, MODEL,
                            stamp.isoformat(), len(candles), len(samples),
                            result.evaluated, result.updates, result.brier,
                            result.baseline_brier, result.accuracy,
                            int(result.candidate_beats_baseline),
                        ),
                    )
                completed += 1
        _LOG.info("RivaL automatic shadow learning evaluated %d streams/horizons", completed)
        return completed

    def latest(self, *, asset: str) -> list[dict]:
        """Read stored evaluation status; no training or exchange calls."""
        if asset not in ("BTC", "ETH", "SOL"):
            raise ValueError("unsupported asset")
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,contract,quote,asset,horizon_minutes,model_version,
                          evaluated_at,causal_examples,evaluated,matured_updates,
                          brier,baseline_brier,directional_accuracy,
                          candidate_beats_baseline
                   FROM perp_shadow_evaluations
                   WHERE asset=? AND model_version=?
                   ORDER BY evaluated_at DESC,venue,horizon_minutes LIMIT 30""",
                (asset, MODEL),
            ).fetchall()
        return [dict(row) for row in rows]
