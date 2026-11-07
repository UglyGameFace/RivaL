"""Runtime serving set for the archive-serving model (archive_v1).

Spec §2.2/§2.8, Phase 2: the offline-trained archive artifact enters the
bot through the Drive candidate import loop, is stored here (one row per
(asset, horizon, artifact)), and issues shadow forecasts beside the
incumbent every 5-minute cycle. It never serves until Route B promotes it
(`family_governance.selected()`), and the kill switch in
``forward_audit.ISSUE_ARCHIVE_V1_CHALLENGERS`` stops issuance.

The artifact document is validated by ``research.archive_training`` on
import: provider hashes (never normalized hashes), feature contract and
digest, calibration evaluated, offline gates passed. The Drive-manifest
byte cross-check that the historical-candidate path applies
(``fetch_manifests_for_payload``) only understands historical-candidate
documents, so it does not apply to archive documents: the import verifies
the artifact's own provenance claims, and the missing byte-level
manifest cross-check is a known Phase 2 limitation, not a silent pass.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime

from parlay_bot.research import archive_training

_LOG = logging.getLogger(__name__)

FAMILY = "archive_v1"
SCHEMA = archive_training.SCHEMA


def model_sha256(doc: dict) -> str:
    """Artifact SHA: canonical JSON of the document (the pooled precedent)."""
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_archive_document(payload: bytes) -> bool:
    return b'"rival-archive-model-v1"' in payload[:4096] or \
        b'"schema":"rival-archive-model-v1"' in payload


class ArchiveModelSet:
    """Imported archive models in the database; serves the newest usable one.

    Mirrors ``PooledModelSet``: one stored row per trained file, newest
    usable document per (asset, horizon) wins. A retrain is a new row under
    the same stable family name; the ``selected()`` continuity check
    (``artifact_identity``) is what lets it keep serving.
    """

    REFRESH_SECONDS = 600

    def __init__(self, store) -> None:
        self.store = store
        self._ready = False
        self._active: dict[tuple[str, int], dict] = {}
        self._loaded_at: float | None = None
        self._signature: tuple | None = None
        self._pending_usable = True

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_archive_models (
                    asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
                    model_sha256 TEXT NOT NULL, trained_through TEXT NOT NULL,
                    gates_passed INTEGER NOT NULL, imported_at TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(asset, horizon_minutes, model_sha256)
                )"""
            )
        self._ready = True

    def imported_file_ids(self) -> frozenset[str]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT drive_file_id FROM rival_archive_models"
            ).fetchall()
        return frozenset(str(r["drive_file_id"]) for r in rows)

    def import_document(self, file_id: str, payload: bytes) -> int:
        """Validate and store one artifact; 0 when already imported.

        ``validate()`` raises ValueError on any artifact that must not
        enter the import path (wrong schema/family/contract/digest,
        non-finite weights, uncalibrated, missing provider hashes, a
        provider hash that is the normalized hash, gates not passed).
        """
        doc = archive_training.validate(json.loads(payload))
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """INSERT INTO rival_archive_models
                   (asset,horizon_minutes,model_sha256,trained_through,
                    gates_passed,imported_at,drive_file_id,document)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (doc["asset"], int(doc["horizon_minutes"]), model_sha256(doc),
                 str(doc["trained_through"]),
                 int(doc.get("gates_passed") is True),
                 datetime.now(UTC).isoformat(), file_id, payload.decode()),
            )
        self._loaded_at = None
        return row.rowcount

    def active(self, asset: str, horizon: int, *,
               now: datetime | None = None) -> dict | None:
        """Newest usable artifact for (asset, horizon), or None."""
        if (self._loaded_at is None
                or time.monotonic() - self._loaded_at > self.REFRESH_SECONDS):
            self._reload(now)
        return self._active.get((asset, int(horizon)))

    def serving(self, asset: str, horizon: int, *,
                now: datetime | None = None) -> list[dict]:
        doc = self.active(asset, horizon, now=now)
        return [doc] if doc is not None else []

    def _reload(self, now) -> None:
        self.initialize()
        order = """FROM rival_archive_models
                   WHERE gates_passed=1
                   ORDER BY trained_through DESC, imported_at DESC"""
        with self.store.connect() as conn:
            signature = tuple(
                (str(r["asset"]), int(r["horizon_minutes"]),
                 str(r["model_sha256"]))
                for r in conn.execute(
                    "SELECT asset, horizon_minutes, model_sha256 " + order
                ).fetchall()
            )
            if (signature == self._signature and not self._pending_usable):
                self._loaded_at = time.monotonic()
                return
            rows = conn.execute(
                "SELECT asset, horizon_minutes, document " + order
            ).fetchall()
        pending_usable = False
        active: dict[tuple[str, int], dict] = {}
        for row in rows:
            key = (str(row["asset"]), int(row["horizon_minutes"]))
            if key in active:
                continue
            try:
                doc = archive_training.validate(json.loads(row["document"]))
            except (ValueError, TypeError, KeyError):
                continue
            if not archive_training.usable(doc, now=now):
                # Trained through a time not yet reached: it becomes usable
                # later without any row changing, so keep reloading in full.
                pending_usable = pending_usable or doc.get("gates_passed") is True
                continue
            clean = {k: v for k, v in doc.items() if not k.startswith("_")}
            # Hash the canonical document BEFORE annotating it: the
            # stored _sha256 must equal the hash of the document as
            # published (the pooled precedent).
            sha = model_sha256(clean)
            clean["_family"] = FAMILY
            clean["_sha256"] = sha
            active[key] = clean
        self._active = active
        self._signature = signature
        self._pending_usable = pending_usable
        self._loaded_at = time.monotonic()


def archive_calls(model_set, *, asset, horizon, vector,
                  now: datetime | None = None) -> list[dict]:
    """One shadow call for the active archive artifact, or none.

    ``vector`` is the (values, flags) tuple from
    ``archive_features.live_vector`` for this stream's candles. No call
    when there is no usable artifact, when the vector is unavailable, or
    when every feature group is unavailable (the flow_v3 skip rule).
    Never raises: an unavailable challenger is an empty list, never a
    failed incumbent cycle.
    """
    if model_set is None or vector is None:
        return []
    try:
        values, flags = vector
        if all(float(f) == 0 for f in flags):
            return []
        doc = model_set.active(asset, horizon, now=now)
        if doc is None:
            return []
        # The active document carries _family/_sha256 annotations; the
        # stored _sha256 is the canonical-document hash (the pooled
        # precedent), so use it rather than rehashing the annotated doc.
        return [{"family": FAMILY,
                 "probability": archive_training.probability(doc, values),
                 "training_rows": int(doc.get("training_rows") or 0),
                 "model_sha256": doc.get("_sha256") or model_sha256(doc)}]
    except (ValueError, KeyError, TypeError, OverflowError):
        _LOG.warning("RivaL archive model call unavailable asset=%s horizon=%s",
                     asset, horizon)
        return []


def artifact_identity(store, asset: str, horizon: int) -> dict | None:
    """Currently-imported artifact for (asset, horizon), or None.

    The continuity identity for the ``selected()`` branch (spec §2.2):
    feature digest, weights SHA recomputed from the stored weights (never
    trusted from a stored column), trained-through, and the graded count
    read live from the shadow ledger -- the number of settled archive_v1
    rows for this (asset, horizon). A retrained artifact is accepted only
    when that count actually advanced since promotion.
    """
    try:
        doc = ArchiveModelSet(store).active(asset, horizon)
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    if doc is None:
        return None
    try:
        identity = archive_training.artifact_identity(doc)
    except (ValueError, KeyError, TypeError):
        return None
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                   AND settled_at IS NOT NULL AND outcome_up IN (0,1)""",
                (FAMILY, asset, int(horizon)),
            ).fetchone()
        graded = int(row["n"]) if row is not None else 0
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    return {**identity, "graded_count": graded}


def calibration_status(store, asset: str, horizon: int) -> dict:
    """Calibration record for receipts; unevaluated until an artifact ships."""
    try:
        doc = ArchiveModelSet(store).active(asset, horizon)
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        doc = None
    if doc is None:
        return {"method": "platt", "status": "unevaluated"}
    calibration = dict(doc.get("calibration") or {})
    calibration.setdefault("method", "platt")
    calibration.setdefault("status", "unevaluated")
    return calibration
