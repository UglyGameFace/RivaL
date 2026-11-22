"""Materialized scorecard reads: precomputed descriptive payloads.

Slice 1 of the out-of-process scope (2026-11-21): the heavy descriptive
aggregates behind the Performance surfaces (forward scorecard, calibration,
active-learning evidence, Polymarket scorecard) are computed by a separate
worker process on a fixed schedule and stored here as point-lookup rows.
Bot readers may serve a row only behind the default-off
``RIVAL_MATERIALIZED_READS`` flag and only while it is fresh; every miss,
stale row, or error falls back to the exact live computation, so a dead
worker degrades the bot to precisely its previous behavior.

Only descriptive dashboards use this table. Issuance, settlement,
promotion, and admission never read it; they keep their authoritative
ledgers.

Payloads are stored as canonical JSON produced by :func:`canonical_json`.
Plain ``json`` cannot round-trip two shapes the forward scorecard returns
-- a ``datetime`` (``as_of``) and int-keyed dicts (``last_7d[15]``) -- so
the codec tags those (and tuples) and :func:`decode_payload` restores the
exact Python values, types included. Floats round-trip exactly (repr is a
round-trip representation), so a decoded payload compares ``==`` to the
live-computed dict.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime

_LOG = logging.getLogger(__name__)

KIND_FORWARD_SUMMARY = "forward_summary"
KIND_FORWARD_CALIBRATION = "forward_calibration"
KIND_ACTIVE_LEARNING = "active_learning"
KIND_POLYMARKET_SUMMARY = "polymarket_summary"

SHADOW_PREFIX = "shadow:"
FRESHNESS_SECONDS = 900

_DT_TAG = "$dt"
_INTKEYS_TAG = "$intkeys"
_TUPLE_TAG = "$tuple"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_materialized_reads (
    kind TEXT NOT NULL,
    asset TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (kind, asset, horizon_minutes)
);
"""

_UPSERT = """
INSERT INTO rival_materialized_reads
    (kind, asset, horizon_minutes, computed_at, payload_json)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (kind, asset, horizon_minutes)
DO UPDATE SET computed_at = excluded.computed_at,
              payload_json = excluded.payload_json
"""

_SELECT = """
SELECT computed_at, payload_json FROM rival_materialized_reads
WHERE kind = ? AND asset = ? AND horizon_minutes = ?
"""


def shadow_kind(kind: str) -> str:
    return SHADOW_PREFIX + kind


def _encode(value):
    if isinstance(value, datetime):
        return {_DT_TAG: value.isoformat()}
    if isinstance(value, tuple):
        return {_TUPLE_TAG: [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: _encode(item) for key, item in value.items()}
        return {_INTKEYS_TAG: [[key, _encode(item)] for key, item in
                               sorted(value.items(), key=lambda kv: kv[0])]}
    return value


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {_DT_TAG}:
            return datetime.fromisoformat(value[_DT_TAG])
        if set(value) == {_TUPLE_TAG}:
            return tuple(_decode(item) for item in value[_TUPLE_TAG])
        if set(value) == {_INTKEYS_TAG}:
            return {key: _decode(item) for key, item in value[_INTKEYS_TAG]}
        return {key: _decode(item) for key, item in value.items()}
    return value


def canonical_json(payload) -> str:
    """Deterministic JSON text for a scorecard payload (byte-identity unit)."""
    return json.dumps(_encode(payload), sort_keys=True, separators=(",", ":"))


def decode_payload(payload_json: str):
    """Inverse of :func:`canonical_json`: restores datetimes and int keys."""
    return _decode(json.loads(payload_json))


def payload_sha256(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class MaterializedReads:
    """Point-lookup store for precomputed descriptive payloads."""

    def __init__(self, store) -> None:
        self.store = store
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def initialize(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self.store.connect() as conn:
                conn.executescript(_SCHEMA)
            self._schema_ready = True

    def upsert(self, kind: str, asset: str, horizon_minutes: int,
               payload, *, computed_at: datetime) -> None:
        self.initialize()
        stamp = computed_at.astimezone(UTC).isoformat()
        with self.store.connect() as conn:
            conn.execute(_UPSERT, (kind, asset, int(horizon_minutes), stamp,
                                   canonical_json(payload)))

    def read_row(self, kind: str, asset: str, horizon_minutes: int = 0):
        """(computed_at, payload_json) for the key, or None. Never raises."""
        try:
            with self.store.connect() as conn:
                row = conn.execute(
                    _SELECT, (kind, asset, int(horizon_minutes))).fetchone()
        except Exception:  # noqa: BLE001 - a missing table is just a miss
            return None
        if row is None:
            return None
        try:
            stamp = datetime.fromisoformat(str(row["computed_at"]))
        except (TypeError, ValueError):
            return None
        if stamp.tzinfo is None:
            return None
        return stamp, str(row["payload_json"])

    def read_fresh(self, kind: str, asset: str, horizon_minutes: int = 0, *,
                   now: datetime | None = None,
                   max_age_seconds: float = FRESHNESS_SECONDS):
        """Decoded payload when a fresh row exists, else None. Never raises."""
        found = self.read_row(kind, asset, horizon_minutes)
        if found is None:
            return None
        stamp, payload_json = found
        wall = now or datetime.now(UTC)
        if abs((wall - stamp).total_seconds()) > max_age_seconds:
            return None
        try:
            return decode_payload(payload_json)
        except (TypeError, ValueError):
            return None


_CREATION_LOCK = threading.Lock()


def materialized_reads(store) -> MaterializedReads:
    with _CREATION_LOCK:
        reads = getattr(store, "_materialized_reads", None)
        if reads is None:
            reads = MaterializedReads(store)
            store._materialized_reads = reads
        return reads


_ENABLED_LOCK = threading.Lock()
_ENABLED: bool | None = None


def reads_enabled() -> bool:
    """The RIVAL_MATERIALIZED_READS flag (Settings, default off).

    Resolved once per process: on Discloud an env change only reaches the
    container on a deploy/restart anyway, so caching cannot serve a flag
    value the operator did not deploy.
    """
    global _ENABLED
    if _ENABLED is None:
        with _ENABLED_LOCK:
            if _ENABLED is None:
                try:
                    from parlay_bot.config import Settings

                    _ENABLED = bool(Settings().rival_materialized_reads)
                except Exception:  # noqa: BLE001 - flag unreadable = off
                    _ENABLED = False
    return _ENABLED


def reset_reads_enabled_cache() -> None:
    """Test hook: forget the resolved flag so the next read re-resolves it."""
    global _ENABLED
    with _ENABLED_LOCK:
        _ENABLED = None


def lookup_if_enabled(store, kind: str, asset: str, horizon_minutes: int = 0, *,
                      now: datetime | None = None):
    """Fresh materialized payload when the flag is on, else None.

    The single entry point bot readers use: flag off, missing table, stale
    row, and corrupt payload all return None, and the caller falls back to
    its exact live computation. Never raises.
    """
    if not reads_enabled():
        return None
    try:
        return materialized_reads(store).read_fresh(
            kind, asset, horizon_minutes, now=now)
    except Exception:  # noqa: BLE001 - fallback covers every failure shape
        _LOG.debug("materialized read unavailable", exc_info=True)
        return None
