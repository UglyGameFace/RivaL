"""Last good copy of each slow Bot Performance read, kept in the database.

The page's slow reads (7-day scorecards, the improvement check, the trial
table) are cached in memory and refreshed in the background. Two things
still made them show "couldn't load": every restart empties the memory
caches, and while the 5-minute cycle holds most of the 8 database
connections a cold read can wait past its limit. Each good result is now
also saved here, and when a live read fails the page shows that saved copy
with its age instead of a blank. Older than ``MAX_AGE`` is never shown.
"""
from __future__ import annotations

import base64
import logging
import pickle
import threading
import time
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

TABLE = "rival_screen_snapshots"
MAX_AGE = timedelta(hours=6)
SAVE_EVERY_SECONDS = 300          # per key: the background refresh is every 30 min

_LOCK = threading.Lock()
_MEM: dict[str, tuple[datetime, object]] = {}
_SAVED_AT: dict[str, float] = {}
_STORE = None


def key(name: str, asset: str, horizon: int) -> str:
    return f"{name}|{asset}|{int(horizon)}"


def _ddl(conn) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        snapshot_key TEXT PRIMARY KEY, saved_at TEXT NOT NULL, payload TEXT NOT NULL)""")


def load(store, now: datetime | None = None) -> int:
    """Remember the store and read back every recent copy (once per start)."""
    global _STORE
    _STORE = store
    cutoff = ((now or datetime.now(UTC)) - MAX_AGE).isoformat()
    loaded = 0
    try:
        with store.connect() as conn:
            _ddl(conn)
            rows = conn.execute(
                f"SELECT snapshot_key, saved_at, payload FROM {TABLE} WHERE saved_at>=?",
                (cutoff,)).fetchall()
    except Exception:  # noqa: BLE001 - the fallback is optional
        _LOG.warning("Saved Bot Performance copies unavailable", exc_info=True)
        return 0
    for row in rows:
        try:
            value = pickle.loads(base64.b64decode(row["payload"]))  # our own rows
            saved = datetime.fromisoformat(str(row["saved_at"]))
        except Exception:  # noqa: BLE001 - a copy from older code is skipped
            _LOG.debug("Skipped an unreadable saved copy %s", row["snapshot_key"])
            continue
        with _LOCK:
            if row["snapshot_key"] not in _MEM:
                _MEM[row["snapshot_key"]] = (saved, value)
                loaded += 1
    return loaded


def remember(name: str, asset: str, horizon: int, value: object) -> None:
    """Keep a good result in memory, and in the database every few minutes."""
    k = key(name, asset, horizon)
    stamp = datetime.now(UTC)
    with _LOCK:
        _MEM[k] = (stamp, value)
        due = time.monotonic() - _SAVED_AT.get(k, -1e9) >= SAVE_EVERY_SECONDS
        if due:
            _SAVED_AT[k] = time.monotonic()
    store = _STORE
    if not due or store is None:
        return
    try:
        payload = base64.b64encode(pickle.dumps(value)).decode()
        with store.connect() as conn:
            conn.execute(
                f"""INSERT INTO {TABLE} (snapshot_key, saved_at, payload) VALUES (?,?,?)
                    ON CONFLICT (snapshot_key) DO UPDATE
                    SET saved_at=excluded.saved_at, payload=excluded.payload""",
                (k, stamp.isoformat(), payload))
    except Exception:  # noqa: BLE001 - the fallback is optional
        _LOG.info("Could not save a Bot Performance copy for %s", k, exc_info=True)


def last(name: str, asset: str, horizon: int, now: datetime | None = None):
    """(value, age) of the last good copy, or None if none recent enough."""
    with _LOCK:
        hit = _MEM.get(key(name, asset, horizon))
    if hit is None:
        return None
    age = (now or datetime.now(UTC)) - hit[0]
    return None if age > MAX_AGE else (hit[1], age)


def reset() -> None:
    """Tests only."""
    global _STORE
    with _LOCK:
        _MEM.clear()
        _SAVED_AT.clear()
    _STORE = None
