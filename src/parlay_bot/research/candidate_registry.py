"""Verified historical weights with explicit feature and outcome contracts.

Legacy candidates remain isolated. Matching, screened artifacts can seed the
live research learner; prospective performance and trade promotion are separate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.markets import ASSETS, HORIZONS, asset_for_contract
from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT, TARGET_CONTRACT
from parlay_bot.research.boosted_portable import validate_portable
from parlay_bot.storage.hot import RelationalHotStore


class CandidateValidationError(ValueError):
    """External research candidate cannot be trusted or is unsupported."""


def verified_candidate(payload: bytes) -> dict:
    if len(payload) > 2_200_000 or not payload:
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
        asset = asset_for_contract(contract)
        horizon = int(spec["horizon_minutes"])
        boosted = spec.get("model") == "portable-boosted-binary-v1"
        weights = spec.get("weights")
        months = spec["source_sha256_by_month"]
        declared = str(source["model_sha256"])
        canonical = json.dumps(
            spec, sort_keys=True, separators=(",", ":"),
        ).encode()
        actual = hashlib.sha256(canonical).hexdigest()
        validation = source["validation"]
        holdout = source["out_of_sample_test"]
        compatible = spec["model"] == "offline-live-contract-logistic-v3"
        if boosted:
            model = validate_portable(spec["portable_model"])
            if (spec.get("family") != model["family"]
                or spec.get("feature_contract") != FEATURE_CONTRACT
                or spec.get("target_contract") != TARGET_CONTRACT
                or identity.get("interval_minutes") != 5
                or type(spec.get("training_count")) is not int
                or spec["training_count"] < 300
                or type(spec.get("native_parity_max_error")) not in (int, float)
                or not 0 <= spec["native_parity_max_error"] <= 1e-6
                or datetime.fromisoformat(spec["train_cutoff"]).utcoffset() is None
                or not (datetime.fromisoformat(spec["train_cutoff"])
                        < datetime.fromisoformat(spec["validation_cutoff"])
                        < datetime.fromisoformat(spec["evaluation_completed_through"]))):
                raise CandidateValidationError("incompatible native-parity tree artifact")
        if compatible and (
            spec.get("feature_contract") != FEATURE_CONTRACT
            or spec.get("target_contract") != TARGET_CONTRACT
            or identity.get("interval_minutes") != 5
            or type(spec.get("training_count")) is not int or spec["training_count"] < 300
            or datetime.fromisoformat(spec["training_cutoff"]).utcoffset() is None
            or not (datetime.fromisoformat(spec["training_cutoff"]) <
                    datetime.fromisoformat(spec["validation_cutoff"]) <
                    datetime.fromisoformat(spec["evaluation_completed_through"]))
        ):
            raise CandidateValidationError("incompatible live research contract")
        if (
            spec["model"] not in ("offline-logistic-multi-horizon-v1",
                                  "offline-live-contract-logistic-v3",
                                  "portable-boosted-binary-v1")
            or (identity["venue"] != "binance"
                and (not (compatible or boosted)
                     or identity["venue"] not in {
                         "hyperliquid", "dydx", "bitget", "gate", "okx",
                         "kucoin", "deribit", "lighter", "phemex",
                     }))
            or (identity["quote"] != "USDT"
                and (not (compatible or boosted)
                     or identity["quote"] not in {"USDC", "USD"}))
            or asset not in ASSETS
            or horizon not in (HORIZONS if (compatible or boosted)
                               else (5, 15, 60, 240, 1440, 10080))
            or (not boosted and (
                not isinstance(weights, list) or len(weights) != (11 if compatible else 6)
                or not all(type(w) in (float, int) and math.isfinite(w) for w in weights)
            ))
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
            and (not (compatible or boosted) or (
                type(validation.get("ece_10")) in (float, int)
                and type(holdout.get("ece_10")) in (float, int)
                and 0 <= validation["ece_10"] <= .10
                and 0 <= holdout["ece_10"] <= .10
                and validation.get("days", 0) >= 7 and holdout.get("days", 0) >= 7
            ))
        )
        # Explain screening without silently relaxing any retrospective gate.
        if passed:
            screen_reason = "retrospective_pass"
        elif len(months) < 6:
            screen_reason = "fewer_than_six_source_months"
        elif validation["count"] < 100 or holdout["count"] < 100:
            screen_reason = "insufficient_purged_validation_or_holdout"
        elif (validation.get("brier_gain") or 0) <= .005:
            screen_reason = "validation_brier_gain_below_0.005"
        elif (holdout.get("brier_gain") or 0) <= .005:
            screen_reason = "holdout_brier_gain_below_0.005"
        else:
            screen_reason = "calibration_or_evaluation_days_below_gate"
        return {
            "admission_reason": screen_reason,
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
            "compatible": compatible or boosted,
            "model_spec": spec,
        }
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise CandidateValidationError(
            "invalid checksum-verified candidate document",
        ) from exc


class ResearchCandidateRegistry:
    """Immutable offline-candidate inventory, isolated from promotion tables."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store
        self._initialized = False
        self._lock = threading.RLock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            self._initialize()
            self._initialized = True

    def _initialize(self) -> None:
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
            conn.execute("""CREATE TABLE IF NOT EXISTS rival_offline_model_artifacts (
                model_sha256 TEXT PRIMARY KEY, model_spec_json TEXT NOT NULL,
                compatible INTEGER NOT NULL, imported_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS rival_history_selection (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL,evaluated_at TEXT NOT NULL,
                selected_sha256 TEXT,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,
                PRIMARY KEY(venue,contract,quote,horizon_minutes)
            )""")

    def import_verified(self, file_id: str, payload: bytes, *,
                        now: datetime | None = None) -> dict:
        self.initialize()
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
            inserted = conn.execute(
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
            conn.execute(
                "INSERT INTO rival_offline_model_artifacts "
                "(model_sha256,model_spec_json,compatible,imported_at) VALUES (?,?,?,?) "
                "ON CONFLICT(model_sha256) DO NOTHING",
                (candidate["model_sha256"], json.dumps(candidate["model_spec"],
                 sort_keys=True, separators=(",", ":"), allow_nan=False),
                 int(candidate["compatible"]), timestamp),
            )
        if inserted.rowcount == 1:
            identity = candidate["model_spec"]["identity"]
            logging.getLogger(__name__).info(
                "RivaL verified model imported sha256=%s venue=%s "
                "contract=%s quote=%s horizon=%d model=%s "
                "feature_contract=%s retrospective_screen=%s reason=%s",
                candidate["model_sha256"], identity["venue"],
                identity["contract"], identity["quote"], candidate["horizon"],
                candidate["model_spec"]["model"],
                candidate["model_spec"].get("feature_contract", "legacy"),
                candidate["retrospective_screen"], candidate["admission_reason"],
            )
        return candidate

    def import_document(self, file_id: str, payload: bytes, *, now=None) -> int:
        """Accept the original checksummed 42-model bundle or individual files."""
        if len(payload) > 2_200_000:
            raise CandidateValidationError("oversized candidate document")
        try:
            source = json.loads(payload)
            if "candidate_models" not in source:
                self.import_verified(file_id, payload, now=now)
                return 1
            if (source.get("schema_version") != "rival-offline-candidate-registry-v1"
                or source.get("historical_only") is not True
                or source.get("approved_to_serve_live_forecasts") is not False
                or source.get("approved_to_trade") is not False
                or not isinstance(source["candidate_models"], list)
                or not 1 <= len(source["candidate_models"]) <= 100):
                raise CandidateValidationError("invalid historical bundle")
            documents = [{**v, "research_only": True, "live_model_promoted": False}
                         for v in source["candidate_models"]]
            # Validate the whole document before writing any member.
            for doc in documents:
                verified_candidate(json.dumps(doc).encode())
            for doc in documents:
                self.import_verified(file_id, json.dumps(doc).encode(), now=now)
            return len(documents)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CandidateValidationError("invalid historical model document") from exc

    def inventory_receipt(self) -> dict:
        """Actual persisted totals, not the count of Drive re-downloads."""
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE c.retrospective_screen='passed')
                              AS passed,
                          COUNT(*) FILTER (WHERE a.compatible=1) AS compatible
                   FROM rival_offline_candidates c
                   JOIN rival_offline_model_artifacts a
                     ON a.model_sha256=c.model_sha256"""
            ).fetchone()
        return {
            "total": int(row["total"]),
            "passed": int(row["passed"]),
            "compatible": int(row["compatible"]),
        }

    def compatible_candidates(self, *, venue, contract, quote, horizon, now):
        """Load a matching training-only artifact once, outside the hot path.

        Holdout failures and future-trained artifacts can never supply weights.
        Reading a candidate here is permission for shadow research inference,
        not a qualification to trade or a claim of live superiority.
        """
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT c.model_sha256,a.model_spec_json,c.holdout_brier,c.first_imported_at FROM "
                "rival_offline_candidates c JOIN rival_offline_model_artifacts a "
                "ON a.model_sha256=c.model_sha256 WHERE a.compatible=1 "
                "AND c.retrospective_screen='passed' AND c.horizon_minutes=? AND c.asset=? "
                "ORDER BY c.first_imported_at DESC,c.model_sha256 LIMIT 30",
                (horizon, asset_for_contract(contract)),
            ).fetchall()
        found = []
        for row in rows:
            spec = json.loads(row["model_spec_json"])
            sha = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            identity = spec["identity"]
            if (spec.get("model") == "offline-live-contract-logistic-v3"
                and sha == row["model_sha256"] and
                (identity["venue"], identity["contract"], identity["quote"]) == (venue, contract, quote)
                and datetime.fromisoformat(spec["evaluation_completed_through"]) < now
                and datetime.fromisoformat(row["first_imported_at"]) <= now):
                found.append({**spec, "model_sha256": sha, "retrospective_screen": "passed",
                              "imported_at": row["first_imported_at"]})
        return found[:2]

    def compatible_boosters(self, *, venue, contract, quote, horizon, now):
        """Source-exact native-parity tree research, never auto-approved.

        An offline model for Binance futures cannot be reused for any other
        exchange or prediction contract. No model is read from unverified bytes.
        """
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT c.model_sha256,a.model_spec_json,c.first_imported_at FROM "
                "rival_offline_candidates c JOIN rival_offline_model_artifacts a "
                "ON a.model_sha256=c.model_sha256 WHERE a.compatible=1 "
                "AND c.retrospective_screen='passed' AND c.horizon_minutes=? AND c.asset=? "
                "ORDER BY c.first_imported_at DESC,c.model_sha256 LIMIT 60",
                (horizon, asset_for_contract(contract)),
            ).fetchall()
        found = {}
        for row in rows:
            try:
                spec = json.loads(row["model_spec_json"])
                if spec.get("model") != "portable-boosted-binary-v1":
                    continue
                sha = hashlib.sha256(json.dumps(
                    spec, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if (sha != row["model_sha256"]
                    or (spec["identity"]["venue"], spec["identity"]["contract"],
                        spec["identity"]["quote"]) != (venue, contract, quote)
                    or datetime.fromisoformat(spec["evaluation_completed_through"]) >= now
                    or datetime.fromisoformat(row["first_imported_at"]) > now):
                    continue
                validate_portable(spec["portable_model"])
                found.setdefault(spec["family"], {
                    "model_sha256": sha, "family": spec["family"],
                    "portable_model": spec["portable_model"],
                    "identity": spec["identity"],
                    "horizon_minutes": spec["horizon_minutes"],
                    "imported_at": row["first_imported_at"],
                    "evaluation_completed_through": spec["evaluation_completed_through"],
                })
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        return list(found.values())

    def compatible_seed(self, **scope):
        found = self.compatible_candidates(**scope)
        return found[0] if found else None

    def summary(self, *, asset: str, horizon: int, live_venues=None) -> dict:
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
            inventory = conn.execute(
                "SELECT c.model_sha256,c.retrospective_screen,a.compatible,a.model_spec_json "
                "FROM rival_offline_candidates c LEFT JOIN rival_offline_model_artifacts a "
                "ON a.model_sha256=c.model_sha256 WHERE c.asset=? AND c.horizon_minutes=?",
                (asset, horizon),
            ).fetchall()
            selected = conn.execute(
                "SELECT * FROM rival_history_selection WHERE contract=? AND horizon_minutes=? "
                "ORDER BY evaluated_at DESC LIMIT 1", (asset+'USDT', horizon),
            ).fetchone()
        compatible = [r for r in inventory if r["compatible"] == 1]
        passed = [r for r in compatible if r["retrospective_screen"] == "passed"]
        matched = [r for r in passed if live_venues is None or
                   json.loads(r["model_spec_json"])["identity"]["venue"] in live_venues]
        # A selection for a disabled or different source is not an active
        # historical seed, even if its previous result remains in the ledger.
        active = bool(
            selected and selected["selected_sha256"]
            and timedelta(0) <= datetime.now(UTC)-datetime.fromisoformat(selected["evaluated_at"])
            < timedelta(hours=48)
            and any(
                r["model_sha256"] == selected["selected_sha256"]
                and tuple(json.loads(r["model_spec_json"])["identity"][key]
                          for key in ("venue", "contract", "quote"))
                == tuple(selected[key] for key in ("venue", "contract", "quote"))
                for r in matched
            )
        )
        if not inventory:
            seed_reason = "No historical models imported for this coin/window"
        elif not compatible:
            seed_reason = "Imported models use different live inputs"
        elif not passed:
            seed_reason = "Historical candidates failed the offline accuracy screen"
        elif not matched:
            seed_reason = "Screened historical source has no configured live candle collector"
        elif not active:
            seed_reason = "Historical candidates await sufficient future baseline wins"
        else:
            seed_reason = "Historical challenger selected on paired future outcomes"
        return {"total": int(total), "candidate": dict(row) if row else None,
                "seed_reason": seed_reason, "slot_imported": len(inventory),
                "slot_compatible": len(compatible), "slot_passed": len(passed),
                "slot_live_source_matched": len(matched),
                "active_offline_model": active,
                "historical_selection": dict(selected) if selected else None,
                "reason": selected["reason"] if selected else "No paired-forward-qualified compatible historical challenger"}
