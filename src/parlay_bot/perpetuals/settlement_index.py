"""Settlement-index tick recording for the fair-value pre-test.

The settlement-faithful fair-value family (design 2026-11-19) prices each
15-minute Up/Down contract off the venue's own settlement index: the
Chainlink TWAP-60s for Polymarket, CF Benchmarks BRTI for Kalshi. Neither
series is reconstructable after the fact -- no venue publishes a free
historical archive -- so if it is not recorded live, it does not exist.
Until now nothing recorded either: the PolyBolt client held TWAP ticks in
a bounded in-memory deque and persisted nothing, and no BRTI code existed
at all.

This module is the shared persistence half of both collectors. A tick
sink is handed the ticks the venue clients already receive; the recorder
keeps only the newest tick per (venue, asset) in memory and a flush --
driven by a bot loop, off the stream's receive path -- persists at most
one row per asset per throttle interval (house default 10s, inside the
design's ~5-10s band). Rows are the raw venue prints: source timestamp,
receipt timestamp, index value, the venue's own trailing 60-second
average when the venue publishes one (Kalshi does, PolyBolt's TWAP value
already IS a 60-second average so its avg_60s stays NULL), and a receipt
hash of the source frame. Nothing is repaired, interpolated or carried
forward: a gap in the table is a gap in the record, and the evaluator
fails closed on it.

Collection only: no model reads this table, no forecast uses it, no
order is placed. The single consumer is the read-only fair-value
evaluator (``fair_value_evaluation``).
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

VENUES = ("polymarket", "kalshi")
ASSETS = ("BTC", "ETH", "SOL", "XRP")
# Defence in depth at the sink: parsers already bound tick age, but a
# tick that reaches the recorder stale or future-dated is dropped, not
# persisted as a live print.
MAX_TICK_AGE = timedelta(minutes=3)
MAX_TICK_FUTURE = timedelta(seconds=3)
# Rows per fetchmany chunk when streaming evaluator-side reads (the #727
# pattern): identical ORDER BY order and contents to fetchall(), without
# materializing a whole series before the first row is processed.
FETCH_CHUNK = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_settlement_index_ticks (
    venue TEXT NOT NULL, asset TEXT NOT NULL,
    source_at TEXT NOT NULL, received_at TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    avg_60s DOUBLE PRECISION,
    receipt_sha256 TEXT NOT NULL,
    PRIMARY KEY (venue, asset, source_at)
);
CREATE INDEX IF NOT EXISTS idx_settlement_index_series
    ON rival_settlement_index_ticks (venue, asset, source_at);
"""


@dataclass(frozen=True)
class IndexTick:
    """One settlement-index print, as received from the venue's stream."""

    venue: str
    asset: str
    source_at: datetime
    received_at: datetime
    value: float
    avg_60s: float | None
    receipt_sha256: str


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("index tick clock requires timezone")
    return value.astimezone(UTC).isoformat()


def _valid_price(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    return level if math.isfinite(level) and level > 0 else None


def frame_receipt(channel: str, fields: dict) -> str:
    """Receipt hash of the exact source frame fields (not a signature)."""
    evidence = json.dumps(
        {"channel": channel, **fields},
        sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(evidence).hexdigest()


class SettlementIndexRecorder:
    """Throttled, idempotent storage for settlement-index ticks.

    ``note()`` is called on the venue clients' receive paths and never
    touches the database -- it only replaces the pending tick for its
    (venue, asset). ``flush()`` runs on a bot loop thread and persists
    each pending tick whose source time has advanced at least
    ``throttle_seconds`` past the last persisted tick for that series,
    so the stored series is evenly thinned to the throttle cadence
    while its newest print is never more than one interval stale.
    """

    def __init__(self, store, *, throttle_seconds: int = 10):
        if throttle_seconds < 1:
            raise ValueError("settlement index throttle must be positive")
        self.store = store
        self.throttle_seconds = throttle_seconds
        self._lock = threading.RLock()
        self._ready = False
        self._pending: dict[tuple[str, str], IndexTick] = {}
        self._persisted_at: dict[tuple[str, str], datetime] = {}

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.store.connect() as conn:
                conn.executescript(_SCHEMA)
            self._ready = True

    def note(self, tick: IndexTick) -> None:
        """Offer one received tick; only the newest per series is kept."""
        if tick.venue not in VENUES or tick.asset not in ASSETS:
            return
        if _valid_price(tick.value) is None:
            return
        if tick.source_at.utcoffset() is None or tick.received_at.utcoffset() is None:
            return
        if tick.received_at - tick.source_at > MAX_TICK_AGE \
                or tick.source_at - tick.received_at > MAX_TICK_FUTURE:
            return
        with self._lock:
            key = (tick.venue, tick.asset)
            pending = self._pending.get(key)
            if pending is not None and tick.source_at <= pending.source_at:
                return
            self._pending[key] = tick

    def flush(self) -> int:
        """Persist due pending ticks; returns the number of rows written."""
        self.initialize()
        written = 0
        with self._lock:
            due: list[IndexTick] = []
            for key, tick in list(self._pending.items()):
                last = self._persisted_at.get(key)
                if last is None or (
                        tick.source_at - last).total_seconds() >= self.throttle_seconds:
                    due.append(tick)
            if not due:
                return 0
            with self.store.connect() as conn:
                for tick in due:
                    cursor = conn.execute(
                        """INSERT INTO rival_settlement_index_ticks
                           (venue, asset, source_at, received_at, value,
                            avg_60s, receipt_sha256)
                           VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT DO NOTHING""",
                        (tick.venue, tick.asset, _iso(tick.source_at),
                         _iso(tick.received_at), float(tick.value),
                         (float(tick.avg_60s)
                          if _valid_price(tick.avg_60s) is not None else None),
                         tick.receipt_sha256),
                    )
                    written += cursor.rowcount
            # State advances only after the write has succeeded: if the
            # insert raised, the pending ticks and the throttle clock
            # are untouched, so the next flush retries the same ticks
            # instead of having permanently lost them.
            for tick in due:
                key = (tick.venue, tick.asset)
                self._persisted_at[key] = tick.source_at
                if self._pending.get(key) is tick:
                    del self._pending[key]
        return written

    def ticks(self, *, venue: str, asset: str) -> list[dict]:
        """Stored ticks for one series, in source-time order."""
        self.initialize()
        rows: list[dict] = []
        with self.store.connect() as conn:
            cursor = conn.execute(
                """SELECT * FROM rival_settlement_index_ticks
                   WHERE venue=? AND asset=? ORDER BY source_at""",
                (venue, asset),
            )
            while True:
                chunk = cursor.fetchmany(FETCH_CHUNK)
                if not chunk:
                    break
                rows.extend(dict(row) for row in chunk)
        return rows

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
