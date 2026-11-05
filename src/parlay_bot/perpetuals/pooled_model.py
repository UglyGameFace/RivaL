"""Dependency-free evaluation of RivaL's pooled multi-year tree model.

The model file is plain JSON produced by parlay_bot.research.pooled_training
(LightGBM trees in a compact array form). Training refuses to publish unless
this evaluator reproduces LightGBM's own probabilities on held-out rows.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime

from parlay_bot.perpetuals.pooled_features import FEATURE_SETS, FEATURE_VERSION

SCHEMA = "rival-pooled-model-v1"
MAX_TREES = 1000
MAX_NODES = 200_000
_ZERO = 1e-35
# Node: [feature, threshold, default_left, missing_type, left, right]; leaf: [value]
# missing_type: 0 = None (NaN treated as 0), 1 = Zero, 2 = NaN.


def validate(doc: dict) -> dict:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not a pooled model document")
    expected = FEATURE_SETS.get(doc.get("feature_version"))
    if expected is None or tuple(doc.get("features", ())) != expected:
        raise ValueError("pooled model built for different inputs")
    width = len(expected)
    if doc.get("horizon_minutes") not in (5, 15, 60):
        raise ValueError("unsupported pooled model horizon")
    trees = doc.get("trees")
    if not isinstance(trees, list) or not 1 <= len(trees) <= MAX_TREES:
        raise ValueError("invalid pooled tree count")
    total = 0
    for tree in trees:
        if not isinstance(tree, list) or not tree:
            raise ValueError("invalid pooled tree")
        total += len(tree)
        for node in tree:
            if len(node) == 1:
                if not math.isfinite(node[0]):
                    raise ValueError("invalid leaf")
                continue
            feature, threshold, default_left, missing, left, right = node
            if (type(feature) is not int or not 0 <= feature < width
                    or not math.isfinite(threshold) or default_left not in (0, 1)
                    or missing not in (0, 1, 2)
                    or not (0 < left < len(tree) and 0 < right < len(tree))):
                raise ValueError("invalid pooled split")
    if total > MAX_NODES:
        raise ValueError("oversized pooled model")
    return doc


def raw_score(doc: dict, vector) -> float:
    total = 0.0
    for tree in doc["trees"]:
        index = 0
        while True:
            node = tree[index]
            if len(node) == 1:
                total += node[0]
                break
            feature, threshold, default_left, missing, left, right = node
            value = vector[feature]
            nan = math.isnan(value)
            if missing != 2 and nan:
                value, nan = 0.0, False
            if (missing == 1 and abs(value) <= _ZERO) or (missing == 2 and nan):
                index = left if default_left else right
            else:
                index = left if value <= threshold else right
    return total


def probability(doc: dict, vector) -> float:
    if len(vector) != len(doc["features"]):
        raise ValueError("wrong pooled feature count")
    score = max(-50.0, min(50.0, raw_score(doc, vector)))
    return 1 / (1 + math.exp(-score))


def model_sha256(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


VENUE_SOURCE = "rival_venue_candles"


def recipe(doc: dict) -> dict:
    """What is being evaluated: inputs, horizon, training data and settings."""
    out = {k: doc.get(k) for k in ("schema", "family", "feature_version", "features",
                                   "horizon_minutes", "params")}
    # Added only when present so every existing family keeps its name.
    if doc.get("training_sources"):
        out["training_sources"] = sorted(doc["training_sources"])
    return out


def lineage(doc: dict) -> str:
    """'venues' when trained on RivaL's own exchange candles too, else 'binance'."""
    return "venues" if VENUE_SOURCE in (doc.get("training_sources") or ()) else "binance"


def family(doc: dict) -> str:
    """Challenger name for the shadow ledger: one per RECIPE, so nightly
    retrains on one more day of data keep a single, growing live record.
    The exact trained model stays traceable through model_sha256."""
    digest = hashlib.sha256(json.dumps(recipe(doc), sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return f"pooled_{doc['horizon_minutes']}m_{digest[:12]}"


def usable(doc: dict, *, now: datetime | None = None) -> bool:
    """Only models trained strictly before now and that passed their gates."""
    try:
        trained = datetime.fromisoformat(str(doc["trained_through"]))
        trained = trained if trained.utcoffset() else trained.replace(tzinfo=UTC)
        return (doc.get("gates_passed") is True
                and trained < (now or datetime.now(UTC)))
    except (KeyError, ValueError, TypeError):
        return False


class PooledModelSet:
    """Imported pooled models in the database; serves the newest usable one."""

    REFRESH_SECONDS = 600

    def __init__(self, store) -> None:
        self.store = store
        self._ready = False
        # Keyed (lineage, horizon): each training lineage runs side by side
        # so its own live record decides, never a silent replacement.
        self._active: dict[tuple[str, int], dict] = {}
        self._fallback: dict[tuple[str, int], dict] = {}  # newest price-only (v1)
        self._loaded_at: float | None = None

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_pooled_models (
                    family TEXT PRIMARY KEY, horizon_minutes INTEGER NOT NULL,
                    model_sha256 TEXT NOT NULL, trained_through TEXT NOT NULL,
                    gates_passed INTEGER NOT NULL, imported_at TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL, document TEXT NOT NULL
                )"""
            )
        self._ready = True

    @staticmethod
    def is_pooled_document(payload: bytes) -> bool:
        return b'"rival-pooled-model-v1"' in payload[:4096] or \
            b'"schema":"rival-pooled-model-v1"' in payload

    def import_document(self, file_id: str, payload: bytes) -> int:
        doc = validate(json.loads(payload))
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """INSERT INTO rival_pooled_models
                   (family,horizon_minutes,model_sha256,trained_through,gates_passed,
                    imported_at,drive_file_id,document)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                # Row key: one per trained file (the recipe name repeats nightly).
                (f"pooled_{doc['horizon_minutes']}m_{model_sha256(doc)[:12]}",
                 doc["horizon_minutes"], model_sha256(doc),
                 str(doc["trained_through"]), int(doc.get("gates_passed") is True),
                 datetime.now(UTC).isoformat(), file_id, payload.decode()),
            )
        self._loaded_at = None
        return row.rowcount

    def active(self, horizon: int, *, now: datetime | None = None,
               context: bool = True, lineage: str = "binance") -> dict | None:
        """Newest usable model; without live context, the newest price-only one."""
        import time

        if self._loaded_at is None or time.monotonic() - self._loaded_at > self.REFRESH_SECONDS:
            self._reload(now)
        doc = self._active.get((lineage, horizon))
        if doc is not None and not context and doc.get("feature_version") != FEATURE_VERSION:
            return self._fallback.get((lineage, horizon))
        return doc

    def serving(self, horizon: int, *, now: datetime | None = None,
                context: bool = True) -> list[dict]:
        """One model per training lineage for this horizon (Binance-only first)."""
        return [doc for name in ("binance", "venues")
                if (doc := self.active(horizon, now=now, context=context,
                                       lineage=name)) is not None]

    def _reload(self, now) -> None:
        import time

        self.initialize()
        active: dict[tuple[str, int], dict] = {}
        fallback: dict[tuple[str, int], dict] = {}
        order = """FROM rival_pooled_models
                   WHERE gates_passed=1 ORDER BY trained_through DESC, imported_at DESC"""
        with self.store.connect() as conn:
            # Every 10 minutes this re-downloaded every model document
            # (~0.7 MB each, ~10 MB in all: 0.7-3 s). Documents are immutable
            # per model_sha256, so when the ordered set of models is unchanged
            # and none was waiting to become usable, the selection is too.
            signature = tuple(
                (str(r["family"]), str(r["model_sha256"]))
                for r in conn.execute("SELECT family, model_sha256 " + order).fetchall()
            )
            if (signature == getattr(self, "_signature", None)
                    and not getattr(self, "_pending_usable", True)):
                self._loaded_at = time.monotonic()
                return
            rows = conn.execute("SELECT horizon_minutes, document " + order).fetchall()
        pending_usable = False
        for row in rows:
            horizon = int(row["horizon_minutes"])
            try:
                doc = validate(json.loads(row["document"]))
            except (ValueError, TypeError, KeyError):
                continue
            key = (lineage(doc), horizon)
            if key in active and key in fallback:
                continue
            if not usable(doc, now=now):
                # Trained through a time not yet reached: it becomes usable
                # later without any row changing, so keep reloading in full.
                pending_usable = pending_usable or doc.get("gates_passed") is True
                continue
            clean = {k: v for k, v in doc.items() if not k.startswith("_")}
            doc["_family"], doc["_sha256"] = family(clean), model_sha256(clean)
            active.setdefault(key, doc)
            if doc.get("feature_version") == FEATURE_VERSION:
                fallback.setdefault(key, doc)
        self._active = active
        self._fallback = fallback
        self._signature = signature
        self._pending_usable = pending_usable
        self._loaded_at = time.monotonic()
