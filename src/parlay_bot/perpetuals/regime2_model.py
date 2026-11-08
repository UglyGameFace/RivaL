"""Runtime serving set for the regime_v2 challenger (trending-subset logistic).

Spec rival-regime-v2-spec-20261108, D3/D5/D8/D10: the offline-trained
regime_v2 artifact enters the bot through the Drive candidate import
loop, is stored here (one row per (asset, horizon, artifact)), and
issues shadow forecasts beside the incumbent every 5-minute cycle --
but only when the regime gate passes AND a pinned artifact exists.
It never serves until Route B promotes it (``family_governance.
selected()``), and the kill switch in
``forward_audit.ISSUE_REGIME_V2_CHALLENGERS`` stops issuance.

The artifact document is validated by ``research.regime2_training``
on import: provider hashes (never normalized hashes), feature
contract and digest (which pins the trending-subset descriptor),
calibration evaluated, offline gates passed. The Drive-manifest byte
cross-check that the historical-candidate path applies
(``fetch_manifests_for_payload``) only understands historical-candidate
documents, so it does not apply to regime_v2 documents: the import
verifies the artifact's own provenance claims, and the missing
byte-level manifest cross-check is a known limitation (same as
archive_v1 / gbm_v1), not a silent pass.

Pinning (D14): the exact fit under Route B evaluation holds the
shadow slot until it is promoted or rejected-with-finding.
``active()`` returns the pinned artifact, not the newest usable one;
a newly trained fit waits in the queue (see ``queued()``) and starts
its own 21 days / 150 targets from zero when admitted. The graded
count in ``artifact_identity()`` is filtered by the pinned artifact's
document hash, so a new fit never inherits evidence it did not earn.

Regime_v1 keeps issuing throughout as the control: v2 is measured
against the incumbent AND against v1 on the same targets.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import offline_model_pins
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.regime_gate import trending_gate
from parlay_bot.research import regime2_training

_LOG = logging.getLogger(__name__)

FAMILY = "regime_v2"
SCHEMA = regime2_training.SCHEMA

# The trial window the Performance page reports over (same as pooled/v1).
TRIAL_DAYS = 7


def model_sha256(doc: dict) -> str:
    """Artifact SHA: canonical JSON of the document (the pooled precedent)."""
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def is_regime2_document(payload: bytes) -> bool:
    return b'"rival-regime2-model-v1"' in payload[:4096] or \
        b'"schema":"rival-regime2-model-v1"' in payload


class Regime2ModelSet:
    """Imported regime_v2 models in the database; serves the pinned one.

    Mirrors ``ArchiveModelSet``/``GbmModelSet``: one stored row per
    trained file. Import takes the newest usable artifact, but the
    shadow slot serves the pinned artifact (D14) -- see
    ``offline_model_pins`` for the pin rules.
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
                """CREATE TABLE IF NOT EXISTS rival_regime2_models (
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
                "SELECT drive_file_id FROM rival_regime2_models"
            ).fetchall()
        return frozenset(str(r["drive_file_id"]) for r in rows)

    def import_document(self, file_id: str, payload: bytes) -> int:
        """Validate and store one artifact; 0 when already imported.

        ``validate()`` raises ValueError on any artifact that must not
        enter the import path (wrong schema/family/contract/digest,
        wrong subset descriptor, non-finite weights, uncalibrated,
        missing provider hashes, a provider hash that is the normalized
        hash, gates not passed).
        """
        doc = regime2_training.validate(json.loads(payload))
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """INSERT INTO rival_regime2_models
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
        order = """FROM rival_regime2_models
                   WHERE asset=? AND horizon_minutes=? AND gates_passed=1
                   ORDER BY trained_through DESC, imported_at DESC"""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT document " + order, (asset, int(horizon))
            ).fetchall()
        docs = []
        for row in rows:
            try:
                doc = regime2_training.validate(json.loads(row["document"]))
            except (ValueError, TypeError, KeyError):
                continue
            if not regime2_training.usable(doc, now=now):
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
        order = """FROM rival_regime2_models
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
                doc = regime2_training.validate(json.loads(row["document"]))
            except (ValueError, TypeError, KeyError):
                continue
            if not regime2_training.usable(doc, now=now):
                # Trained through a time not yet reached: it becomes usable
                # later without any row changing, so keep reloading in full.
                pending_usable = pending_usable or doc.get("gates_passed") is True
                continue
            clean = {k: v for k, v in doc.items() if not k.startswith("_")}
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


def regime2_calls(model_set, *, asset, horizon, vector, candles,
                  now: datetime | None = None) -> list[dict]:
    """One shadow call for the pinned regime_v2 artifact, or none.

    ``vector`` is the (values, flags) tuple from
    ``archive_features.live_vector`` for this stream's candles (the same
    32-feature vector the archive logistic uses). No call when the gate
    abstains (not trending), when there is no pinned artifact, when the
    vector is unavailable, or when every feature group is unavailable
    (the flow_v3 skip rule). Never raises: an unavailable challenger
    is an empty list, never a failed incumbent cycle.
    """
    if model_set is None or vector is None:
        return []
    try:
        if not trending_gate(candles):
            return []
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
                 "probability": regime2_training.probability(doc, values),
                 "training_rows": int(doc.get("training_rows") or 0),
                 "model_sha256": doc.get("_sha256") or model_sha256(doc)}]
    except (ValueError, KeyError, TypeError, OverflowError):
        _LOG.warning("RivaL regime_v2 model call unavailable asset=%s horizon=%s",
                     asset, horizon)
        return []


def artifact_identity(store, asset: str, horizon: int) -> dict | None:
    """Pinned artifact for (asset, horizon), or None.

    The pinning identity for the ``selected()`` branch (spec D3/D5,
    D14): feature digest, weights SHA recomputed from the stored
    weights (never trusted from a stored column), trained-through,
    and the graded count read live from the shadow ledger -- the
    number of settled regime_v2 rows for this (asset, horizon) under
    the pinned artifact's document hash only. A queued fit's rows
    never count toward the pinned fit.
    """
    try:
        doc = Regime2ModelSet(store).active(asset, horizon)
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    if doc is None:
        return None
    try:
        identity = regime2_training.artifact_identity(doc)
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
        doc = Regime2ModelSet(store).active(asset, horizon)
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        doc = None
    if doc is None:
        return {"method": "platt", "status": "unevaluated"}
    calibration = dict(doc.get("calibration") or {})
    calibration.setdefault("method", "platt")
    calibration.setdefault("status", "unevaluated")
    return calibration


def trial_report(store, *, asset, horizon, days=TRIAL_DAYS, now=None) -> dict:
    """D5 trial report for regime_v2 over the last `days` days.

    issued / graded / decided / hits / windows / model_brier /
    incumbent_brier / baseline_brier are the same columns as the pooled
    trial row, measured on regime_v2's issued subset only. The
    decomposition is the extra part, and for v2 the paired gain is the
    headline (unlike v1, where it is 0.000 by construction):
      paired_gain            = incumbent Brier - v2 Brier on the subset
                               (positive: v2 beat the incumbent on the
                               targets it actually issued on);
      paired_gain_vs_regime_v1 = regime_v1 Brier - v2 Brier on the
                               targets both families issued on (the
                               head-to-head: did v2 add anything over
                               v1?);
      selection_effect       = incumbent Brier on the subset - incumbent
                               Brier on ALL targets in the same window
                               (positive: the subset was harder than
                               average for the incumbent; negative:
                               easier);
      coverage               = issued v2 rows / issued incumbent rows
                               in the window;
      availability_ratio     = issued v2 rows / issued regime_v1 rows
                               in the window (how often the artifact was
                               there when the gate passed).
    Never raises: returns {"issued": 0} on any read failure.
    """
    stamp = utc(now or datetime.now(UTC))
    since = (stamp - timedelta(days=days)).isoformat()
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS issued,
                          SUM(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL THEN 1 ELSE 0 END) AS graded,
                          SUM(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1) THEN 1 ELSE 0 END) AS decided,
                          SUM(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   AND ((probability_up >= 0.5) = (outcome_up = 1))
                                   THEN 1 ELSE 0 END) AS hits,
                          COUNT(DISTINCT CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   THEN target_opened_at END) AS windows,
                          AVG(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   THEN model_brier END) AS model_brier,
                          AVG(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   THEN incumbent_brier END) AS incumbent_brier,
                          AVG(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   AND baseline_brier IS NOT NULL
                                   THEN baseline_brier END) AS baseline_brier,
                          MIN(issued_at) AS first_issued
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family=?
                     AND issued_at>=?""",
                (asset, int(horizon), FAMILY, since),
            ).fetchone()
            incumbent = conn.execute(
                """SELECT COUNT(*) AS issued,
                          AVG(model_brier) AS model_brier
                   FROM perp_forward_audit
                   WHERE asset=? AND horizon_minutes=? AND model_brier IS NOT NULL
                     AND issued_at>=?""",
                (asset, int(horizon), since),
            ).fetchone()
            # regime_v1 on the same targets v2 issued on: the
            # head-to-head is a mean-Brier difference over the shared
            # target set (same granularity as the "windows" count
            # above: one target window, one observation).
            v1_row = conn.execute(
                """SELECT AVG(model_brier) AS model_brier
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family='regime_v1'
                     AND settled_at IS NOT NULL AND outcome_up IS NOT NULL
                     AND probability_up NOT IN (0,1)
                     AND issued_at>=?
                     AND target_opened_at IN (
                         SELECT DISTINCT target_opened_at
                         FROM rival_model_shadow_forecasts
                         WHERE asset=? AND horizon_minutes=? AND family=?
                           AND settled_at IS NOT NULL
                           AND outcome_up IS NOT NULL
                           AND probability_up NOT IN (0,1)
                           AND issued_at>=?)""",
                (asset, int(horizon), since,
                 asset, int(horizon), FAMILY, since),
            ).fetchone()
            # Availability denominator: every regime_v1 row issued in
            # the window (v1 issues whenever the gate passes, so this
            # is the gate-pass count the v2 artifact had to match).
            v1_issued_row = conn.execute(
                """SELECT COUNT(*) AS issued
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family='regime_v1'
                     AND issued_at>=?""",
                (asset, int(horizon), since),
            ).fetchone()
    except Exception:  # noqa: BLE001 - a failed read is an empty report
        return {"issued": 0}
    issued = int(row["issued"] or 0)
    if issued == 0:
        return {"issued": 0}
    model_brier = row["model_brier"]
    incumbent_brier = row["incumbent_brier"]
    all_incumbent_brier = incumbent["model_brier"]
    paired_gain = (
        None
        if model_brier is None or incumbent_brier is None
        else float(incumbent_brier) - float(model_brier)
    )
    v1_brier = v1_row["model_brier"]
    paired_gain_vs_regime_v1 = (
        None
        if model_brier is None or v1_brier is None
        else float(v1_brier) - float(model_brier)
    )
    selection_effect = (
        None
        if incumbent_brier is None or all_incumbent_brier is None
        else float(incumbent_brier) - float(all_incumbent_brier)
    )
    incumbent_issued = int(incumbent["issued"] or 0)
    v1_issued = int(v1_issued_row["issued"] or 0)
    return {
        "issued": issued,
        "graded": int(row["graded"] or 0),
        "decided": int(row["decided"] or 0),
        "hits": int(row["hits"] or 0),
        "windows": int(row["windows"] or 0),
        "model_brier": model_brier,
        "incumbent_brier": incumbent_brier,
        "baseline_brier": row["baseline_brier"],
        "first_issued": row["first_issued"],
        "paired_gain": paired_gain,
        "paired_gain_vs_regime_v1": paired_gain_vs_regime_v1,
        "selection_effect": selection_effect,
        "coverage": (issued / incumbent_issued) if incumbent_issued else None,
        "availability_ratio": (issued / v1_issued) if v1_issued else None,
    }
