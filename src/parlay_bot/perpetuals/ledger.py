"""Persist immutable forecasts and learn from verified, delayed outcomes.

Uses the existing RivaL relational store. No unverified outcome trains weights.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from parlay_bot.perpetuals.fusion import FusedForecast, SourceSkill, finite, utc

if TYPE_CHECKING:
    from parlay_bot.storage.hot import RelationalHotStore


@dataclass(frozen=True)
class PredictionPerformance:
    count: int
    brier: float | None
    directional_accuracy: float | None


class PredictionLedger:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_predictions (
                    id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    contract_kind TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    issued_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    reference_source TEXT NOT NULL,
                    reference_price DOUBLE PRECISION NOT NULL,
                    probability_up DOUBLE PRECISION NOT NULL,
                    agreement DOUBLE PRECISION NOT NULL,
                    model_version TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    settled_outcome INTEGER,
                    settled_at TEXT,
                    settlement_price DOUBLE PRECISION,
                    ensemble_brier DOUBLE PRECISION
                );
                CREATE INDEX IF NOT EXISTS perp_predictions_due
                    ON perp_predictions (due_at, settled_outcome);
                CREATE TABLE IF NOT EXISTS perp_source_skill (
                    asset TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    score_count INTEGER NOT NULL,
                    brier_sum DOUBLE PRECISION NOT NULL,
                    recent_count DOUBLE PRECISION NOT NULL,
                    recent_brier_sum DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (asset, quote, origin, horizon_minutes)
                );
                """
            )

    def skill_for(self, asset: str, quote: str, origin: str, horizon: int) -> SourceSkill:
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT score_count, brier_sum, recent_count, recent_brier_sum
                   FROM perp_source_skill
                   WHERE asset=? AND quote=? AND origin=? AND horizon_minutes=?""",
                (asset, quote, origin, horizon),
            ).fetchone()
        if row is None:
            return SourceSkill()
        return SourceSkill(
            int(row["score_count"]), float(row["brier_sum"]),
            float(row["recent_count"]), float(row["recent_brier_sum"]),
        )

    def record(self, forecast: FusedForecast) -> bool:
        """An idempotent insert must never overwrite a published prediction."""
        if (
            len(forecast.contributors) < 3
            or len(forecast.contributors) != len(forecast.weights)
            or len({x.origin for x in forecast.contributors}) != len(forecast.contributors)
            or len({x.source for x in forecast.contributors}) != len(forecast.contributors)
            or any(not finite(w, 0, 0.40000001) for w in forecast.weights)
            or not math.isclose(sum(forecast.weights), 1, abs_tol=1e-9)
            or not finite(forecast.probability_up, 0, 1)
            or not finite(forecast.agreement, 0, 1)
            or not finite(forecast.reference_price, 1e-12)
        ):
            raise ValueError("invalid forecast provenance")
        if utc(forecast.due_at) <= utc(forecast.issued_at):
            raise ValueError("invalid horizon timing")
        if not any(
            x.source == forecast.reference_source and x.reference_price == forecast.reference_price
            for x in forecast.contributors
        ):
            raise ValueError("invalid reference source")
        if any(
            (x.asset, x.quote, x.horizon_minutes, x.contract_kind) !=
            (forecast.asset, forecast.quote, forecast.horizon_minutes, forecast.contract_kind)
            or utc(x.observed_at) > utc(forecast.issued_at)
            for x in forecast.contributors
        ):
            raise ValueError("mismatched contributor or future data")
        components = [
            {"source": x.source, "origin": x.origin, "p_up": x.probability_up}
            for x in forecast.contributors
        ]
        with self.store.connect() as conn:
            cur = conn.execute(
                """INSERT INTO perp_predictions
                   (id,asset,quote,contract_kind,horizon_minutes,issued_at,due_at,
                    reference_source,reference_price,probability_up,agreement,
                    model_version,sources_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    forecast.id, forecast.asset, forecast.quote, forecast.contract_kind,
                    forecast.horizon_minutes, utc(forecast.issued_at).isoformat(),
                    utc(forecast.due_at).isoformat(), forecast.reference_source,
                    forecast.reference_price, forecast.probability_up, forecast.agreement,
                    forecast.model_version, json.dumps(components, separators=(",", ":")),
                ),
            )
            return cur.rowcount == 1

    def settle(
        self,
        prediction_id: str,
        *,
        reference_source: str,
        observed_at: datetime,
        settlement_price: float,
        maximum_lag: timedelta = timedelta(minutes=2),
    ) -> bool:
        """Settle once against the original venue near the actual due timestamp."""
        ts = utc(observed_at)
        if not finite(settlement_price, 1e-12) or maximum_lag < timedelta(0):
            raise ValueError("invalid settlement")
        if ts > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("settlement cannot be in the future")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM perp_predictions WHERE id=?", (prediction_id,)
            ).fetchone()
            if row is None:
                raise KeyError("unknown prediction")
            due = utc(datetime.fromisoformat(str(row["due_at"])))
            if ts < due or ts > due + maximum_lag:
                raise ValueError("observation must match the forecast due time")
            if row["reference_source"] != reference_source:
                raise ValueError("wrong reference venue")
            if row["settled_outcome"] is not None:
                return False
            reference_price = float(row["reference_price"])
            if settlement_price == reference_price:
                raise ValueError("flat price is not an up/down outcome")
            outcome = int(settlement_price > reference_price)
            change = conn.execute(
                """UPDATE perp_predictions
                   SET settled_outcome=?,settled_at=?,settlement_price=?,ensemble_brier=?
                   WHERE id=? AND settled_outcome IS NULL""",
                (
                    outcome, ts.isoformat(), settlement_price,
                    (float(row["probability_up"]) - outcome) ** 2, prediction_id,
                ),
            )
            if change.rowcount != 1:
                return False
            for item in json.loads(str(row["sources_json"])):
                loss = (float(item["p_up"]) - outcome) ** 2
                conn.execute(
                    """INSERT INTO perp_source_skill
                       (asset,quote,origin,horizon_minutes,score_count,
                        brier_sum,recent_count,recent_brier_sum)
                       VALUES (?,?,?,?,1,?,1,?)
                       ON CONFLICT (asset,quote,origin,horizon_minutes)
                       DO UPDATE SET
                         score_count=perp_source_skill.score_count+1,
                         brier_sum=perp_source_skill.brier_sum+excluded.brier_sum,
                         recent_count=perp_source_skill.recent_count*0.95+1,
                         recent_brier_sum=perp_source_skill.recent_brier_sum*0.95
                           +excluded.recent_brier_sum""",
                    (row["asset"], row["quote"], item["origin"],
                     row["horizon_minutes"], loss, loss),
                )
            return True

    def performance(
        self, *, asset: str, quote: str, horizon_minutes: int,
        model_version: str = "reliability-fusion-v1", recent: int = 250,
    ) -> PredictionPerformance:
        """Score only settled predictions, never self-reported model accuracy."""
        if not 1 <= recent <= 10000:
            raise ValueError("recent must be between 1 and 10000")
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT probability_up,settled_outcome,ensemble_brier
                   FROM perp_predictions
                   WHERE asset=? AND quote=? AND horizon_minutes=? AND model_version=?
                     AND settled_outcome IS NOT NULL
                   ORDER BY due_at DESC,id DESC LIMIT ?""",
                (asset, quote, horizon_minutes, model_version, recent),
            ).fetchall()
        if not rows:
            return PredictionPerformance(0, None, None)
        directed = [x for x in rows if float(x["probability_up"]) != 0.5]
        correct = sum(
            (float(x["probability_up"]) > 0.5) == bool(x["settled_outcome"])
            for x in directed
        )
        return PredictionPerformance(
            len(rows),
            sum(float(x["ensemble_brier"]) for x in rows) / len(rows),
            correct / len(directed) if directed else None,
        )
