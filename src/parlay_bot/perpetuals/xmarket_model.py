"""Pinned XMarket runtime artifacts; prospective shadow issuance only."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime

from parlay_bot.perpetuals import offline_model_pins
from parlay_bot.research import xmarket_training

_LOG = logging.getLogger(__name__)

FAMILY = "xmarket_v1"
SCHEMA = xmarket_training.SCHEMA


def model_sha256(doc: dict) -> str:
    """Artifact SHA: canonical JSON of the document (the pooled precedent)."""
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_xmarket_document(payload: bytes) -> bool:
    try:
        doc = json.loads(payload)
        return isinstance(doc, dict) and doc.get("schema") == SCHEMA
    except (ValueError, UnicodeError, TypeError):
        return False


class XmarketModelSet:
    """Imported xmarket models in the database; serves the pinned one.

    Mirrors ``ArchiveModelSet``: one stored row per trained file. Import
    takes the newest usable artifact, but the shadow slot serves the
    pinned artifact (D14) -- see ``offline_model_pins`` for the pin rules.
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
                """CREATE TABLE IF NOT EXISTS rival_xmarket_models (
                    asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
                    model_sha256 TEXT NOT NULL, trained_through TEXT NOT NULL,
                    gates_passed INTEGER NOT NULL, imported_at TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(asset, horizon_minutes, model_sha256)
                )"""
            )
        offline_model_pins.initialize(self.store)
        self._ready = True

    def imported_file_ids(self) -> frozenset[str]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT drive_file_id FROM rival_xmarket_models"
            ).fetchall()
        return frozenset(str(r["drive_file_id"]) for r in rows)

    def import_document(self, file_id: str, payload: bytes) -> int:
        """Validate and store one artifact; 0 when already imported.

        ``validate()`` raises ValueError on any artifact that must not
        enter the import path (wrong schema/family/contract/digest,
        non-finite weights, uncalibrated, missing provider hashes, a
        provider hash that is the normalized hash, gates not passed).
        """
        doc = xmarket_training.validate(json.loads(payload))
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """INSERT INTO rival_xmarket_models
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
        """Pinned artifact for (asset, horizon), or None."""
        if (self._loaded_at is None
                or time.monotonic() - self._loaded_at > self.REFRESH_SECONDS):
            self._reload(now)
        return self._active.get((asset, int(horizon)))

    def serving(self, asset: str, horizon: int, *,
                now: datetime | None = None) -> list[dict]:
        doc = self.active(asset, horizon, now=now)
        return [doc] if doc is not None else []

    def usable_docs(self, asset: str, horizon: int, *,
                    now: datetime | None = None) -> list[dict]:
        """Usable artifacts for (asset, horizon), newest first, annotated."""
        self.initialize()
        order = """FROM rival_xmarket_models
                   WHERE asset=? AND horizon_minutes=? AND gates_passed=1
                   ORDER BY trained_through DESC, imported_at DESC"""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT document " + order, (asset, int(horizon))
            ).fetchall()
        docs = []
        for row in rows:
            try:
                doc = xmarket_training.validate(json.loads(row["document"]))
            except (ValueError, TypeError, KeyError):
                continue
            if not xmarket_training.usable(doc, now=now):
                continue
            clean = {k: v for k, v in doc.items() if not k.startswith("_")}
            # Hash the canonical document BEFORE annotating it: the
            # stored _sha256 must equal the hash of the document as
            # published (the pooled precedent).
            sha = model_sha256(clean)
            clean["_family"] = FAMILY
            clean["_sha256"] = sha
            docs.append(clean)
        return docs

    def queued(self, asset: str, horizon: int, *,
               now: datetime | None = None) -> list[dict]:
        """Usable artifacts waiting behind the pinned one, newest first.

        A rejected fit is not queued: it will never be admitted again.
        """
        active = self.active(asset, horizon, now=now)
        docs = self.usable_docs(asset, horizon, now=now)
        rejected = offline_model_pins.rejected_shas(
            self.store, FAMILY, asset, horizon)
        if active is None:
            return [d for d in docs if d["_sha256"] not in rejected]
        return [d for d in docs
                if d["_sha256"] != active["_sha256"]
                and d["_sha256"] not in rejected]

    def queue_depth(self, asset: str, horizon: int, *,
                    now: datetime | None = None) -> int:
        """Number of usable fits waiting behind the pinned one."""
        return len(self.queued(asset, horizon, now=now))

    def _reload(self, now) -> None:
        self.initialize()
        order = """FROM rival_xmarket_models
                   WHERE gates_passed=1
                   ORDER BY trained_through DESC, imported_at DESC"""
        with self.store.connect() as conn:
            signature = tuple(
                (str(r["asset"]), int(r["horizon_minutes"]),
                 str(r["model_sha256"]))
                for r in conn.execute(
                    "SELECT asset, horizon_minutes, model_sha256 " + order
                ).fetchall()
            ) + offline_model_pins.pin_signature(self.store, FAMILY)
            if (signature == self._signature and not self._pending_usable):
                self._loaded_at = time.monotonic()
                return
            rows = conn.execute(
                "SELECT asset, horizon_minutes, document " + order
            ).fetchall()
        pending_usable = False
        usable_by_key: dict[tuple[str, int], list[dict]] = {}
        for row in rows:
            key = (str(row["asset"]), int(row["horizon_minutes"]))
            try:
                doc = xmarket_training.validate(json.loads(row["document"]))
            except (ValueError, TypeError, KeyError):
                continue
            if not xmarket_training.usable(doc, now=now):
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
            usable_by_key.setdefault(key, []).append(clean)
        active: dict[tuple[str, int], dict] = {}
        for (asset, horizon), docs in usable_by_key.items():
            chosen = offline_model_pins.select_pinned(
                self.store, FAMILY, asset, horizon, docs, now=now)
            if chosen is not None:
                active[(asset, horizon)] = chosen
        self._active = active
        self._signature = signature
        self._pending_usable = pending_usable
        self._loaded_at = time.monotonic()


def xmarket_calls(model_set, *, asset, horizon, vector,
                 now: datetime | None = None) -> list[dict]:
    """One shadow call for the active xmarket artifact, or none.

    ``vector`` is the (values, flags) tuple from
    ``xmarket_features.live_vector`` for this stream's candles. No call
    when there is no usable artifact, when the vector is unavailable, or
    when every feature group is unavailable (the flow_v3 skip rule).
    Never raises: an unavailable challenger is an empty list, never a
    failed incumbent cycle.
    """
    if model_set is None or vector is None:
        return []
    try:
        values, diagnostics = vector
        doc = model_set.active(asset, horizon, now=now)
        if doc is None:
            return []
        # The active document carries _family/_sha256 annotations; the
        # stored _sha256 is the canonical-document hash (the pooled
        # precedent), so use it rather than rehashing the annotated doc.
        return [{"family": FAMILY,
                 "probability": xmarket_training.probability(doc, values),
                 "training_rows": int(doc.get("training_rows") or 0),
                 "model_sha256": doc.get("_sha256") or model_sha256(doc),
                 "diagnostics": diagnostics}]
    except (ValueError, KeyError, TypeError, OverflowError):
        _LOG.warning("RivaL xmarket model call unavailable asset=%s horizon=%s",
                     asset, horizon)
        return []


def artifact_identity(store, asset: str, horizon: int) -> dict | None:
    """Pinned artifact for (asset, horizon), or None.

    The pinning identity for the ``selected()`` branch (spec §3.4, D14):
    feature digest, weights SHA recomputed from the stored weights (never
    trusted from a stored column), trained-through, and the graded count
    read live from the shadow ledger -- the number of settled xmarket_v1
    rows for this (asset, horizon) under the pinned artifact's document
    hash only. A queued fit's rows never count toward the pinned fit.
    """
    try:
        doc = XmarketModelSet(store).active(asset, horizon)
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    if doc is None:
        return None
    try:
        identity = xmarket_training.artifact_identity(doc)
    except (ValueError, KeyError, TypeError):
        return None
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                   AND training_sha256=?
                   AND settled_at IS NOT NULL AND outcome_up IN (0,1)""",
                (FAMILY, asset, int(horizon), doc["_sha256"]),
            ).fetchone()
        graded = int(row["n"]) if row is not None else 0
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    return {**identity, "model_sha256": doc["_sha256"], "graded_count": graded}


def calibration_status(store, asset: str, horizon: int) -> dict:
    """Calibration record for receipts; unevaluated until an artifact ships."""
    try:
        doc = XmarketModelSet(store).active(asset, horizon)
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        doc = None
    if doc is None:
        return {"method": "platt", "status": "unevaluated"}
    calibration = dict(doc.get("calibration") or {})
    calibration.setdefault("method", "platt")
    calibration.setdefault("status", "unevaluated")
    return calibration
