"""Verified historical weights with explicit feature and outcome contracts.

Legacy candidates remain isolated. Matching, screened artifacts can seed the
live research learner; prospective performance and trade promotion are separate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
import weakref
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.markets import ASSETS, HORIZONS, asset_for_contract
from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT, TARGET_CONTRACT
from parlay_bot.research.boosted_portable import validate_portable
from parlay_bot.storage.hot import RelationalHotStore

# The 5-minute cycle asked for the same candidates once per exchange and
# timeframe (about 240 identical reads per cycle, each re-parsing and
# re-hashing up to 60 stored models). The query depends only on coin and
# timeframe, so its parsed rows are kept for ``CANDIDATE_CACHE_SECONDS``;
# every per-call check (exchange, "trained before now", "imported before
# now") still runs on every call. An import clears the cache at once.
CANDIDATE_CACHE_SECONDS = 120.0
_CANDIDATE_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CANDIDATE_LOCK = threading.Lock()


def _cached_rows(store, key, load):
    with _CANDIDATE_LOCK:
        hit = _CANDIDATE_CACHE.get(store, {}).get(key)
        if hit is not None and time.monotonic() - hit[0] < CANDIDATE_CACHE_SECONDS:
            return hit[1]
    rows = load()
    with _CANDIDATE_LOCK:
        _CANDIDATE_CACHE.setdefault(store, {})[key] = (time.monotonic(), rows)
    return rows


def _forget_candidates(store) -> None:
    with _CANDIDATE_LOCK:
        _CANDIDATE_CACHE.pop(store, None)

class CandidateValidationError(ValueError):
    """External research candidate cannot be trusted or is unsupported."""


def verify_source_months(spec: dict, manifests: dict) -> tuple[bool, str]:
    """Cross-check claimed source months against Drive manifests.

    ``manifests`` maps (venue, contract, quote, month) to the Drive
    manifest's parsed JSON for that month. Returns (source_verified, status):
    - ``provider_verified`` -- every claimed month hash equals the manifest's
      declared source hash, and every claimed provider hash equals the
      manifest's provider archive hash.
    - ``normalized_bytes_only`` -- every claimed month hash equals the
      manifest's normalized-data hash instead: the training bytes are proven
      to be the published normalized bytes, but provider provenance is NOT
      proven. Pre-fix rows carry exactly this shape and must never be
      relabelled as provider-verified.
    - ``mismatch:<months>`` / ``manifest_missing:<months>`` -- a claimed
      month matches neither manifest hash, or has no Drive manifest at all.
      Such a candidate is held: it may sit in the inventory, but it must
      never seed research or promote.
    """
    identity = spec.get("identity") or {}
    key_base = (identity.get("venue"), identity.get("contract"),
                identity.get("quote"))
    claimed = spec.get("source_sha256_by_month") or {}
    provider = spec.get("provider_sha256_by_month") or {}
    missing, mismatch, normalized = [], [], []
    for month in sorted(claimed):
        manifest = manifests.get((*key_base, month))
        if not isinstance(manifest, dict):
            missing.append(month)
            continue
        claimed_sha = claimed[month]
        source_sha = manifest.get("source_sha256")
        data_sha = manifest.get("data_sha256")
        archive_sha = manifest.get("source_archive_sha256") or source_sha
        if claimed_sha == source_sha:
            provider_sha = provider.get(month)
            if provider_sha is not None and provider_sha != archive_sha:
                mismatch.append(month)
        elif claimed_sha == data_sha:
            normalized.append(month)
        else:
            mismatch.append(month)
    if missing:
        return False, "manifest_missing:" + ",".join(missing)
    if mismatch:
        return False, "mismatch:" + ",".join(mismatch)
    if normalized:
        return False, "normalized_bytes_only"
    return True, "provider_verified"


def _source_held(verification: str | None) -> bool:
    """Proven-bad provenance: a mismatched month or a missing manifest."""
    return isinstance(verification, str) and verification.startswith(
        ("mismatch:", "manifest_missing:")
    )


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
        provider_months = spec.get("provider_sha256_by_month")
        without_provider = spec.get("months_without_provider_hash")
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
            # Provider hashes are optional (pre-fix specs lack them) but,
            # when present, must name a subset of the claimed months with
            # valid hashes, and must not overlap the no-provider list.
            or (provider_months is not None and (
                not isinstance(provider_months, dict)
                or any(
                    not isinstance(m, str) or m not in months
                    or not isinstance(sha, str) or len(sha) != 64
                    or any(ch not in "0123456789abcdef" for ch in sha)
                    for m, sha in provider_months.items()
                )
            ))
            or (without_provider is not None and (
                not isinstance(without_provider, list)
                or any(
                    not isinstance(m, str) or m not in months
                    or m in (provider_months or {})
                    for m in without_provider
                )
            ))
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
            "provider_sha256_by_month": dict(provider_months or {}),
            "months_without_provider_hash": list(without_provider or []),
            # Shape validation alone never proves provenance. The Drive
            # cross-check at import time sets the real status; without it
            # the candidate stays explicitly not_checked.
            "source_verification": "not_checked",
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
                    admission_reason TEXT,
                    source_verified INTEGER NOT NULL DEFAULT 0,
                    source_verification TEXT,
                    first_imported_at TEXT NOT NULL,
                    active_live INTEGER NOT NULL DEFAULT 0
                )"""
            )
            # One-time migration for existing PostgreSQL and SQLite inventories.
            if "admission_reason" not in self.store.database.column_names(
                conn, "rival_offline_candidates",
            ):
                conn.execute(
                    "ALTER TABLE rival_offline_candidates "
                    "ADD COLUMN admission_reason TEXT"
                )
            if "source_verified" not in self.store.database.column_names(
                conn, "rival_offline_candidates",
            ):
                conn.execute(
                    "ALTER TABLE rival_offline_candidates "
                    "ADD COLUMN source_verified INTEGER NOT NULL DEFAULT 0"
                )
            if "source_verification" not in self.store.database.column_names(
                conn, "rival_offline_candidates",
            ):
                conn.execute(
                    "ALTER TABLE rival_offline_candidates "
                    "ADD COLUMN source_verification TEXT"
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
                        now: datetime | None = None,
                        source_manifests: dict | None = None) -> dict:
        """Admit a checksum-verified candidate, cross-checking provenance.

        ``source_manifests`` maps (venue, contract, quote, month) to the
        Drive manifest's parsed JSON for that month. When supplied, the
        candidate's claimed source months are checked against the actual
        Drive manifests; a mismatch or a missing manifest marks the row
        held (never seedable). When omitted -- no Drive check was performed
        -- the row is stored as ``not_checked``: explicitly unverified,
        never silently relabelled as provider-verified.
        """
        self.initialize()
        candidate = verified_candidate(payload)
        if source_manifests is not None:
            verified, status = verify_source_months(
                candidate["model_spec"], source_manifests,
            )
        else:
            verified, status = False, "not_checked"
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
                    retrospective_screen,admission_reason,
                    source_verified,source_verification,
                    first_imported_at,active_live
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(model_sha256) DO NOTHING""",
                (candidate["model_sha256"], file_id, candidate["asset"],
                 candidate["horizon"], candidate["source_months"],
                 candidate["data_version"], candidate["holdout_accuracy"],
                 candidate["holdout_brier"],
                 candidate["retrospective_screen"],
                 candidate["admission_reason"], int(verified), status,
                 timestamp),
            )
            # Replaying the original verified file may fill a missing legacy
            # reason; never rewrite historical screening, scores or weights.
            if inserted.rowcount == 0:
                conn.execute(
                    """UPDATE rival_offline_candidates SET admission_reason=?
                       WHERE model_sha256=? AND drive_file_id=?
                         AND retrospective_screen=? AND admission_reason IS NULL""",
                    (candidate["admission_reason"], candidate["model_sha256"],
                     file_id, candidate["retrospective_screen"]),
                )
                # A replay WITH a Drive cross-check may fill in a missing
                # verification; a performed check is never overwritten.
                if source_manifests is not None:
                    conn.execute(
                        """UPDATE rival_offline_candidates
                           SET source_verified=?, source_verification=?
                           WHERE model_sha256=? AND drive_file_id=?
                             AND (source_verification IS NULL
                                  OR source_verification='not_checked')""",
                        (int(verified), status, candidate["model_sha256"],
                         file_id),
                    )
            conn.execute(
                "INSERT INTO rival_offline_model_artifacts "
                "(model_sha256,model_spec_json,compatible,imported_at) VALUES (?,?,?,?) "
                "ON CONFLICT(model_sha256) DO NOTHING",
                (candidate["model_sha256"], json.dumps(candidate["model_spec"],
                 sort_keys=True, separators=(",", ":"), allow_nan=False),
                 int(candidate["compatible"]), timestamp),
            )
            # Report the stored verification: on replay the stored check
            # stands, even when this call carried no Drive map.
            stored = conn.execute(
                "SELECT source_verified, source_verification "
                "FROM rival_offline_candidates WHERE model_sha256=?",
                (candidate["model_sha256"],),
            ).fetchone()
            stored_verified = bool(stored["source_verified"])
            stored_status = stored["source_verification"]
        _forget_candidates(self.store)
        if inserted.rowcount == 1:
            identity = candidate["model_spec"]["identity"]
            logging.getLogger(__name__).info(
                "RivaL verified model imported sha256=%s venue=%s "
                "contract=%s quote=%s horizon=%d model=%s "
                "feature_contract=%s retrospective_screen=%s reason=%s "
                "source_verification=%s",
                candidate["model_sha256"], identity["venue"],
                identity["contract"], identity["quote"], candidate["horizon"],
                candidate["model_spec"]["model"],
                candidate["model_spec"].get("feature_contract", "legacy"),
                candidate["retrospective_screen"], candidate["admission_reason"],
                stored_status,
            )
        return {**candidate, "source_verified": stored_verified,
                "source_verification": stored_status}

    def import_document(self, file_id: str, payload: bytes, *, now=None,
                        source_manifests: dict | None = None) -> int:
        """Accept the original checksummed 42-model bundle or individual files."""
        if len(payload) > 2_200_000:
            raise CandidateValidationError("oversized candidate document")
        try:
            source = json.loads(payload)
            if "candidate_models" not in source:
                self.import_verified(file_id, payload, now=now,
                                     source_manifests=source_manifests)
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
                self.import_verified(file_id, json.dumps(doc).encode(), now=now,
                                     source_manifests=source_manifests)
            return len(documents)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CandidateValidationError("invalid historical model document") from exc

    def fully_imported_file_ids(self) -> frozenset[str]:
        """Drive files whose every candidate row is already complete.

        A file with a legacy row still missing its admission reason is left
        out, so replaying it can fill that reason exactly as before.
        """
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT drive_file_id FROM rival_offline_candidates
                   GROUP BY drive_file_id
                   HAVING SUM(CASE WHEN admission_reason IS NULL THEN 1 ELSE 0 END)=0"""
            ).fetchall()
        return frozenset(str(r["drive_file_id"]) for r in rows)

    def inventory_receipt(self) -> dict:
        """Actual persisted totals, not the count of Drive re-downloads."""
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (
                            WHERE a.compatible=1
                              AND c.retrospective_screen='passed'
                          ) AS passed,
                          COUNT(*) FILTER (WHERE a.compatible=1) AS compatible,
                          COUNT(*) FILTER (WHERE a.model_sha256 IS NULL)
                              AS missing_artifacts
                   FROM rival_offline_candidates c
                   LEFT JOIN rival_offline_model_artifacts a
                     ON a.model_sha256=c.model_sha256"""
            ).fetchone()
        return {
            "total": int(row["total"]),
            "passed": int(row["passed"]),
            "compatible": int(row["compatible"]),
            "missing_artifacts": int(row["missing_artifacts"]),
        }

    def compatible_candidates(self, *, venue, contract, quote, horizon, now):
        """Load a matching training-only artifact once, outside the hot path.

        Holdout failures and future-trained artifacts can never supply weights.
        Reading a candidate here is permission for shadow research inference,
        not a qualification to trade or a claim of live superiority.
        """
        self.initialize()
        rows = _cached_rows(self.store, ("candidates", horizon, asset_for_contract(contract)),
                            lambda: self._candidate_rows(horizon, contract))
        found = []
        for spec, sha, row in rows:
            identity = spec["identity"]
            if (spec.get("model") == "offline-live-contract-logistic-v3"
                and sha == row["model_sha256"] and
                (identity["venue"], identity["contract"], identity["quote"]) == (venue, contract, quote)
                and datetime.fromisoformat(spec["evaluation_completed_through"]) < now
                and datetime.fromisoformat(row["first_imported_at"]) <= now):
                found.append({**copy.deepcopy(spec), "model_sha256": sha,
                              "retrospective_screen": "passed",
                              "imported_at": row["first_imported_at"]})
        return found[:2]

    def _candidate_rows(self, horizon, contract):
        """The candidate query, each row parsed and hashed once."""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT c.model_sha256,a.model_spec_json,c.holdout_brier,c.first_imported_at FROM "
                "rival_offline_candidates c JOIN rival_offline_model_artifacts a "
                "ON a.model_sha256=c.model_sha256 WHERE a.compatible=1 "
                "AND c.retrospective_screen='passed' AND c.horizon_minutes=? AND c.asset=? "
                # Proven-bad provenance never seeds: a candidate whose claimed
                # source months mismatch the Drive manifests, or whose months
                # have no Drive manifest at all, is held out of research.
                # Rows never cross-checked (NULL / not_checked) and rows
                # proven to be the published normalized bytes stay eligible;
                # both are labelled unverified, never provider-verified.
                "AND (c.source_verification IS NULL "
                "OR (c.source_verification NOT LIKE 'mismatch%' "
                "AND c.source_verification NOT LIKE 'manifest_missing%')) "
                "ORDER BY c.first_imported_at DESC,c.model_sha256 LIMIT 30",
                (horizon, asset_for_contract(contract)),
            ).fetchall()
        parsed = []
        for row in rows:
            spec = json.loads(row["model_spec_json"])
            sha = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            parsed.append((spec, sha, {k: row[k] for k in ("model_sha256", "first_imported_at")}))
        return parsed

    def compatible_boosters(self, *, venue, contract, quote, horizon, now):
        """Source-exact native-parity tree research, never auto-approved.

        An offline model for Binance futures cannot be reused for any other
        exchange or prediction contract. No model is read from unverified bytes.
        """
        self.initialize()
        rows = _cached_rows(self.store, ("boosters", horizon, asset_for_contract(contract)),
                            lambda: self._booster_rows(horizon, contract))
        found = {}
        for spec, sha, row in rows:
            try:
                if (sha != row["model_sha256"]
                    or (spec["identity"]["venue"], spec["identity"]["contract"],
                        spec["identity"]["quote"]) != (venue, contract, quote)
                    or datetime.fromisoformat(spec["evaluation_completed_through"]) >= now
                    or datetime.fromisoformat(row["first_imported_at"]) > now):
                    continue
                found.setdefault(spec["family"], {
                    "model_sha256": sha, "family": spec["family"],
                    "portable_model": copy.deepcopy(spec["portable_model"]),
                    "identity": dict(spec["identity"]),
                    "horizon_minutes": spec["horizon_minutes"],
                    "imported_at": row["first_imported_at"],
                    "evaluation_completed_through": spec["evaluation_completed_through"],
                })
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        return list(found.values())

    def _booster_rows(self, horizon, contract):
        """The booster query; each row parsed, hashed and validated once."""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT c.model_sha256,a.model_spec_json,c.first_imported_at FROM "
                "rival_offline_candidates c JOIN rival_offline_model_artifacts a "
                "ON a.model_sha256=c.model_sha256 WHERE a.compatible=1 "
                "AND c.retrospective_screen='passed' AND c.horizon_minutes=? AND c.asset=? "
                # Proven-bad provenance never seeds (see compatible_candidates).
                "AND (c.source_verification IS NULL "
                "OR (c.source_verification NOT LIKE 'mismatch%' "
                "AND c.source_verification NOT LIKE 'manifest_missing%')) "
                "ORDER BY c.first_imported_at DESC,c.model_sha256 LIMIT 60",
                (horizon, asset_for_contract(contract)),
            ).fetchall()
        parsed = []
        for row in rows:
            try:
                spec = json.loads(row["model_spec_json"])
                if spec.get("model") != "portable-boosted-binary-v1":
                    continue
                sha = hashlib.sha256(json.dumps(
                    spec, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                validate_portable(spec["portable_model"])
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            parsed.append((spec, sha, {k: row[k] for k in ("model_sha256", "first_imported_at")}))
        return parsed

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
                          retrospective_screen,admission_reason,
                          source_verified,source_verification,first_imported_at
                   FROM rival_offline_candidates
                   WHERE asset=? AND horizon_minutes=?
                   ORDER BY first_imported_at DESC LIMIT 1""",
                (asset, horizon),
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_offline_candidates",
            ).fetchone()["n"]
            inventory = conn.execute(
                "SELECT c.model_sha256,c.retrospective_screen,a.compatible,a.model_spec_json,"
                "c.source_verification "
                "FROM rival_offline_candidates c LEFT JOIN rival_offline_model_artifacts a "
                "ON a.model_sha256=c.model_sha256 WHERE c.asset=? AND c.horizon_minutes=?",
                (asset, horizon),
            ).fetchall()
            # Selection rows carry source-native BTC-USDT-SWAP style names,
            # not the synthetic legacy BTCUSDT contract.
            selections = conn.execute(
                """SELECT * FROM rival_history_selection WHERE horizon_minutes=?
                   ORDER BY evaluated_at DESC LIMIT 300""",
                (horizon,),
            ).fetchall()
            reasons = conn.execute(
                """SELECT COALESCE(admission_reason, 'legacy_reason_unreported')
                          AS reason, COUNT(*) AS n
                   FROM rival_offline_candidates
                   WHERE asset=? AND horizon_minutes=?
                   GROUP BY COALESCE(admission_reason, 'legacy_reason_unreported')
                   ORDER BY n DESC, reason LIMIT 12""",
                (asset, horizon),
            ).fetchall()
        selected = next(
            (r for r in selections
             if asset_for_contract(str(r["contract"])) == asset
             and (live_venues is None or r["venue"] in live_venues)),
            None,
        )
        compatible = [r for r in inventory if r["compatible"] == 1]
        passed = [r for r in compatible if r["retrospective_screen"] == "passed"]
        matched = [r for r in passed if live_venues is None or
                   json.loads(r["model_spec_json"])["identity"]["venue"] in live_venues]
        # Proven-bad provenance can never be the active offline model, even
        # when an earlier selection row still names it.
        matched = [r for r in matched
                   if not _source_held(r["source_verification"])]
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
                "admission_reasons": [
                    {"reason": r["reason"], "count": int(r["n"])} for r in reasons
                ],
                "seed_reason": seed_reason, "slot_imported": len(inventory),
                "slot_compatible": len(compatible), "slot_passed": len(passed),
                "slot_live_source_matched": len(matched),
                "active_offline_model": active,
                "historical_selection": dict(selected) if selected else None,
                "reason": selected["reason"] if selected else "No paired-forward-qualified compatible historical challenger"}
