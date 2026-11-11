"""Bounded windows for repeating data corrections.

Three corrections withdraw results graded from no-trade candles: on the main
model's ledger, on the test-model ledger and on the fused ledger. Each was an
UPDATE over the WHOLE table, every cycle (or every start), although new rows
are already graded correctly at settlement:

- the main model settles a no-trade target as not comparable (forward_audit
  ``no_trade_outcome``), and
- test-model and fused rows only copy outcomes that are 0 or 1.

A row can therefore only need correcting shortly after it settles: its own
target candle, or the main-model result it copied, changed after grading.
Every 5 minutes a correction now checks only rows settled in the last
``RECENT`` (an index range on ``settled_at``). The unbounded sweep still
runs, once per ``FULL_EVERY`` across restarts, recorded in
``rival_maintenance_runs``, so nothing older is ever left uncorrected.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

RECENT = timedelta(days=2)
FULL_EVERY = timedelta(hours=24)

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_maintenance_runs (
    name TEXT PRIMARY KEY, last_full_at TEXT NOT NULL)"""


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("maintenance windows require a UTC-aware clock")
    return value.astimezone(UTC)


def initialize(store) -> None:
    """Create the run table once per store, under the DDL lock.

    executescript takes the shared advisory lock on PostgreSQL, so two
    workers creating it at the same instant cannot collide.
    """
    if getattr(store, "_maintenance_ready", False):
        return
    with store.connect() as conn:
        conn.executescript(SCHEMA)
    try:
        store._maintenance_ready = True
    except AttributeError:
        pass


def correction_cutoff(conn, name: str, now: datetime | None = None) -> str | None:
    """The ``settled_at`` cutoff for this pass, or None for a full sweep.

    None when this correction has never completed a full sweep, or its last
    one is at least ``FULL_EVERY`` old (or in the future: a bad clock never
    suppresses the sweep). Call ``initialize(store)`` first, then this on the
    connection that runs the correction.
    """
    stamp = _utc(now or datetime.now(UTC))
    row = conn.execute(
        "SELECT last_full_at FROM rival_maintenance_runs WHERE name=?", (name,),
    ).fetchone()
    if row is not None:
        try:
            last = _utc(datetime.fromisoformat(str(row["last_full_at"])))
        except (TypeError, ValueError):
            last = None
        if last is not None and timedelta(0) <= stamp - last < FULL_EVERY:
            return (stamp - RECENT).isoformat()
    return None


def record_full_sweep(conn, name: str, now: datetime | None = None) -> None:
    """Record that ``name`` just finished an unbounded sweep."""
    stamp = _utc(now or datetime.now(UTC)).isoformat()
    conn.execute(
        """INSERT INTO rival_maintenance_runs (name,last_full_at) VALUES (?,?)
           ON CONFLICT (name) DO UPDATE SET last_full_at=excluded.last_full_at""",
        (name, stamp),
    )
