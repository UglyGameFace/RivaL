"""Read-only bridge from verified six-month Drive candidates to live visibility.

A historical model's retrospective evaluation is NEVER an approved live model.
It has a different feature/issuance contract from the production forecast.
Import only checksum-verified isolated candidate metadata, not its weights
into the live predictor. Promotion requires a separate prospective scorer.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime

from parlay_bot.perpetuals.markets import ASSETS, HORIZONS
from parlay_bot.storage.hot import RelationalHotStore


class CandidateValidationError(ValueError):
    """External research candidate cannot be trusted or is unsupported."""


def verified_candidate(payload: bytes) -> dict:
    if len(payload) > 131072 or not payload:
        raise CandidateValidationError("oversized or missing candidate")
    try:
        source = json.loads(payload)
        if (
            not isinstance(source, dict)
            or source.get("research_only") is not True
            or source.get("live_model_promoted") is not False
        ):
            raise CandidateValidationError("candidate is not isolated research")
        spec = source["model_spec"]
        identity = spec["identity"]
        contract = str(identity["contract"])
        asset = contract.removesuffix("USDT")
        horizon = int(spec["horizon_minutes"])
        weights = spec["weights"]
        months = spec["source_sha256_by_month"]
        declared = str(source["model_sha256"])
        canonical = json.dumps(
            spec, sort_keys=True, separators=(",", ":"),
        ).encode()
        actual = hashlib.sha256(canonical).hexdigest()
        validation = source["validation"]
        holdout = source["out_of_sample_test"]
        if (
            spec["model"] != "offline-logistic-multi-horizon-v1"
            or identity["venue"] != "binance"
            or identity["quote"] != "USDT"
            or asset not in ASSETS
            or horizon not in HORIZONS
            or not isinstance(weights, list) or len(weights) != 6
            or not all(type(w) in (float, int) and math.isfinite(w) for w in weights)
            or not isinstance(months, dict) or not months
            or any(
                not isinstance(m, str) or len(m) != 7
                or not isinstance(sha, str) or len(sha) != 64
                or any(ch not in "0123456789abcdef" for ch in sha)
                for m, sha in months.items()
            )
            or declared != actual
            or not isinstance(validation, dict)
            or not isinstance(holdout, dict)
        ):
            raise CandidateValidationError("unsupported or corrupt model identity")
        for scores in (validation, holdout):
            if (
                not isinstance(scores.get("count"), int)
                or scores["count"] < 0
                or any(
                    scores.get(k) is not None and (
                        type(scores[k]) not in (float, int)
                        or not math.isfinite(scores[k])
                    )
                    for k in ("brier", "baseline_brier", "brier_gain",
                              "accuracy", "ece_10")
                )
            ):
                raise CandidateValidationError("invalid retrospective evaluation")
        passed = (
            len(months) >= 6
            and validation["count"] >= 100
            and holdout["count"] >= 100
            and (validation.get("brier_gain") or 0) > 0.005
            and (holdout.get("brier_gain") or 0) > 0.005
        )
        return {
            "model_sha256": actual,
            "asset": asset,
            "horizon": horizon,
            "data_version": hashlib.sha256(
                json.dumps(months, sort_keys=True).encode(),
            ).hexdigest(),
            "source_months": len(months),
            "holdout_accuracy": holdout.get("accuracy"),
            "holdout_brier": holdout.get("brier"),
            "retrospective_screen": "passed" if passed else "insufficient",
        }
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise CandidateValidationError(
            "invalid checksum-verified candidate document",
        ) from exc


class ResearchCandidateRegistry:
    """Immutable offline-candidate inventory, isolated from promotion tables."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_offline_candidates (
                    model_sha256 TEXT PRIMARY KEY,
                    drive_file_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    source_months INTEGER NOT NULL,
                    data_version TEXT NOT NULL,
                    holdout_accuracy REAL,
                    holdout_brier REAL,
                    retrospective_screen TEXT NOT NULL,
                    first_imported_at TEXT NOT NULL,
                    active_live INTEGER NOT NULL DEFAULT 0
                )"""
            )

    def import_verified(self, file_id: str, payload: bytes, *,
                        now: datetime | None = None) -> dict:
        candidate = verified_candidate(payload)
        if (
            not file_id or len(file_id) > 200
            or any(ch not in
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                   for ch in file_id)
        ):
            raise CandidateValidationError("invalid Drive model identity")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_offline_candidates (
                    model_sha256,drive_file_id,asset,horizon_minutes,
                    source_months,data_version,holdout_accuracy,holdout_brier,
                    retrospective_screen,first_imported_at,active_live
                ) VALUES (?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(model_sha256) DO NOTHING""",
                (candidate["model_sha256"], file_id, candidate["asset"],
                 candidate["horizon"], candidate["source_months"],
                 candidate["data_version"], candidate["holdout_accuracy"],
                 candidate["holdout_brier"],
                 candidate["retrospective_screen"], timestamp),
            )
        return candidate

    def summary(self, *, asset: str, horizon: int) -> dict:
        if asset not in ASSETS or horizon not in HORIZONS:
            raise ValueError("unsupported research slot")
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT model_sha256,source_months,data_version,
                          holdout_accuracy,holdout_brier,
                          retrospective_screen,first_imported_at
                   FROM rival_offline_candidates
                   WHERE asset=? AND horizon_minutes=?
                   ORDER BY first_imported_at DESC LIMIT 1""",
                (asset, horizon),
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_offline_candidates",
            ).fetchone()["n"]
        return {"total": int(total), "candidate": dict(row) if row else None,
                "active_offline_model": False,
                "reason": "historical feature contract is not the live forecast contract"}
