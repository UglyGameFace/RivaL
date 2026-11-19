"""Executable book snapshots for the PM-lag convergence re-measurement.

The 2026-11-18 falsification pre-test found exactly one surviving slice:
when Polymarket's 15-minute Up price lags Kalshi's by more than the toll,
Polymarket's price tends to converge toward Kalshi's over the next five
minutes. That result was measured at last/mid prices with no depth -- an
upper bound, not executable proof. Re-measuring it needs best bid/ask and
depth for both venues at the t=5 entry and t=10 exit checkpoints, and no
existing store holds that: the CLOB prices-history series is last-trade
only, and every live contract table freezes ONE book at issuance.

This module is the minimal read-only collection that closes the gap. A
bot loop periodically fetches the public books for the currently open
15-minute contracts and stores throttled snapshots here. It is collection
only: no model reads it, no forecast uses it, no order is placed. Books
that fail validation (stale, future, malformed, empty) are dropped, never
repaired or carried forward -- the same fail-closed rule as every other
input in this codebase.

The measurement contract this collection serves (pre-registered in the
2026-11-18 re-measurement report): entry snapshot is the one whose book
time is closest to window open + 300s within +/-45s, exit likewise for
+600s; the traded size is CLIP_SHARES contracts, priced at the
depth-walked average bid/ask, so a snapshot whose book cannot fill the
clip cannot price that leg.

The evaluator also needs each venue's reference level for the window --
Polymarket's price-to-beat and Kalshi's strike -- to prove the two
contracts price the same level before their books are compared. The
first accrual readout (2026-11-19) found every window excluded as
``strike_unavailable``: the watch loop computes Polymarket's
price-to-beat live from Gamma metadata but persisted it nowhere the
evaluator can read, and the oracle/AI-call evidence tables it fell back
to are empty in production. So each snapshot now carries the reference
level its venue had already resolved when the book was fetched
(``reference_level``, nullable): the notice's price-to-beat for
Polymarket, the market payload's ``floor_strike`` for Kalshi. Rows
recorded before this repair keep NULL -- unknown, never backfilled or
approximated from a spot proxy.

The production proof of that repair (2026-11-19) split by venue:
Kalshi snapshots landed their reference 134/134, Polymarket 0/112.
Root cause, verified against the live Gamma API: Gamma publishes
``eventMetadata.priceToBeat`` only AFTER a window closes (absent at
every in-window poll of 5m/15m windows; first present ~20-140s after
close), while the reader paths that build notices only ever read the
current window -- so a Polymarket notice's price-to-beat is None by
construction, and the per-contract cache never sees a value. Nor can
the value ride a post-close book snapshot: the closed contract's book
freezes ~30s after close and fails this module's 25-second freshness
rule before Gamma's publication reliably lands. The collector
therefore resolves each just-closed Polymarket contract's published
price-to-beat with one dedicated Gamma fetch and stores it in
``rival_convergence_reference_levels`` -- its own record, appended
after close; existing snapshot rows are never modified. Kalshi needs
no such path: its ``floor_strike`` is in the live open-markets listing.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import UTC, datetime, timedelta

ASSETS = ("BTC", "ETH", "SOL", "XRP")
CLIP_SHARES = 10.0  # the house reference size for depth proofs
ENTRY_OFFSET_SECONDS = 300
EXIT_OFFSET_SECONDS = 600
CHECKPOINT_TOLERANCE_SECONDS = 45
# Same freshness bound the live Polymarket reader enforces (MAX_BOOK_AGE).
MAX_BOOK_AGE = timedelta(seconds=25)
MAX_BOOK_FUTURE = timedelta(seconds=5)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_convergence_book_snapshots (
    venue TEXT NOT NULL, asset TEXT NOT NULL, contract_id TEXT NOT NULL,
    window_open TEXT NOT NULL, window_close TEXT NOT NULL,
    source_at TEXT NOT NULL, received_at TEXT NOT NULL,
    best_bid DOUBLE PRECISION, best_ask DOUBLE PRECISION,
    bid_size DOUBLE PRECISION, ask_size DOUBLE PRECISION,
    avg_bid DOUBLE PRECISION, avg_ask DOUBLE PRECISION,
    fee_rate DOUBLE PRECISION, reference_level DOUBLE PRECISION,
    book_sha256 TEXT NOT NULL,
    PRIMARY KEY (venue, contract_id, source_at)
);
CREATE INDEX IF NOT EXISTS idx_convergence_quotes_window
    ON rival_convergence_book_snapshots (venue, asset, window_open);
CREATE TABLE IF NOT EXISTS rival_convergence_reference_levels (
    venue TEXT NOT NULL, asset TEXT NOT NULL, contract_id TEXT NOT NULL,
    window_open TEXT NOT NULL, window_close TEXT NOT NULL,
    reference_level DOUBLE PRECISION NOT NULL,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (venue, contract_id)
);
"""


def _ensure_reference_level_column(conn, store) -> None:
    """Idempotently add reference_level to pre-repair snapshot tables.

    CREATE TABLE IF NOT EXISTS cannot extend a table created before the
    reference-level repair, so extend it after the shared DDL runs --
    introspect first, the same additive pattern as history_recovery's
    gap-repair columns (SQLite has no ADD COLUMN IF NOT EXISTS). The
    column stays nullable: rows recorded before the repair keep an
    unknown reference instead of a fabricated one.
    """
    present = store.database.column_names(
        conn, "rival_convergence_book_snapshots")
    if "reference_level" not in present:
        conn.execute(
            "ALTER TABLE rival_convergence_book_snapshots"
            " ADD COLUMN reference_level DOUBLE PRECISION"
        )


def _valid_reference(value) -> float | None:
    """A venue reference level, or None when the value is not evidence.

    Only a finite, positive int/float counts (the shape both venues
    publish); anything else -- missing, a string, a bool, NaN -- leaves
    the snapshot's reference unknown rather than guessed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    return level if math.isfinite(level) and level > 0 else None


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("snapshot clock requires timezone")
    return value.astimezone(UTC).isoformat()


def _walk(levels: list[tuple[float, float]], shares: float) -> float | None:
    """Depth-walked average price for `shares`, or None if the book is short."""
    remaining = shares
    spent = 0.0
    for price, size in levels:
        taken = min(size, remaining)
        spent += taken * price
        remaining -= taken
        if remaining <= 1e-9:
            return spent / shares
    return None


def _clob_levels(book: dict, side: str) -> list[tuple[float, float]]:
    raw = book.get(side)
    if not isinstance(raw, list):
        return []
    levels = []
    for level in raw:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0:
            levels.append((price, size))
    levels.sort(reverse=(side == "bids"))
    return levels


def clob_snapshot(book, *, received_at: datetime) -> dict | None:
    """Best bid/ask + clip depth from one raw CLOB book, or None.

    The book's own timestamp is the source clock: a book older than
    MAX_BOOK_AGE at receipt, dated in the future, or missing its
    timestamp is not evidence and is dropped whole.
    """
    if not isinstance(book, dict) or not book.get("timestamp"):
        return None
    try:
        stamp = float(book["timestamp"])
        if not math.isfinite(stamp):
            return None
        source_at = datetime.fromtimestamp(stamp / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    received = received_at.astimezone(UTC)
    if source_at > received + MAX_BOOK_FUTURE or received - source_at > MAX_BOOK_AGE:
        return None
    bids = _clob_levels(book, "bids")
    asks = _clob_levels(book, "asks")
    if not bids and not asks:
        return None
    return {
        "source_at": source_at,
        "best_bid": bids[0][0] if bids else None,
        "bid_size": bids[0][1] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "ask_size": asks[0][1] if asks else None,
        "avg_bid": _walk(bids, CLIP_SHARES) if bids else None,
        "avg_ask": _walk(asks, CLIP_SHARES) if asks else None,
        "book_sha256": _sha(book),
    }


def _kalshi_levels(book: dict, side: str) -> list[tuple[float, float]]:
    """Kalshi orderbook_fp bid levels for one side, best first.

    Strict like parse_orderbook: a malformed level invalidates the book,
    because a depth claim built on a guessed level is not evidence.
    """
    rows = book.get(side)
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise ValueError("invalid Kalshi orderbook levels")
    levels = []
    for level in rows:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("malformed Kalshi bid level")
        raw_price, raw_size = level
        if not isinstance(raw_price, str) or not isinstance(raw_size, str):
            raise TypeError("Kalshi fixed-point book fields must be strings")
        price, size = float(raw_price), float(raw_size)
        if not (math.isfinite(price) and 0 < price < 1
                and math.isfinite(size) and size > 0):
            raise ValueError("invalid or zero-depth Kalshi bid")
        levels.append((price, size))
    levels.sort(reverse=True)
    return levels


def kalshi_snapshot(payload, *, received_at: datetime) -> dict | None:
    """Executable YES quotes from one Kalshi orderbook_fp payload, or None.

    Kalshi exposes bids only: the YES ask is the complement of the best
    NO bid, and the YES ask depth at each level is the NO bid depth at
    the complementary price. The payload carries no source clock, so the
    receipt time is the snapshot time.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("orderbook_fp"), dict):
        return None
    book = payload["orderbook_fp"]
    try:
        yes_bids = _kalshi_levels(book, "yes_dollars")
        no_bids = _kalshi_levels(book, "no_dollars")
    except (TypeError, ValueError, OverflowError):
        return None
    if not yes_bids and not no_bids:
        return None
    ask_levels = [(1.0 - price, size) for price, size in no_bids]
    return {
        "source_at": received_at.astimezone(UTC),
        "best_bid": yes_bids[0][0] if yes_bids else None,
        "bid_size": yes_bids[0][1] if yes_bids else None,
        "best_ask": ask_levels[0][0] if ask_levels else None,
        "ask_size": ask_levels[0][1] if ask_levels else None,
        "avg_bid": _walk(yes_bids, CLIP_SHARES) if yes_bids else None,
        "avg_ask": _walk(ask_levels, CLIP_SHARES) if ask_levels else None,
        "book_sha256": _sha(payload),
    }


def select_checkpoint(rows, *, window_open: datetime,
                      offset_seconds: int,
                      tolerance_seconds: int = CHECKPOINT_TOLERANCE_SECONDS):
    """The snapshot whose source time is closest to open + offset.

    Rows are snapshot dicts with an ISO `source_at`. Anything outside the
    tolerance is not a checkpoint observation; ties resolve to the
    earlier snapshot so the choice is deterministic.
    """
    target = window_open.astimezone(UTC) + timedelta(seconds=offset_seconds)
    best = None
    best_key = None
    for row in rows:
        try:
            at = datetime.fromisoformat(str(row["source_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if at.utcoffset() is None:
            continue
        delta = abs((at - target).total_seconds())
        if delta > tolerance_seconds:
            continue
        key = (delta, at)
        if best_key is None or key < best_key:
            best, best_key = row, key
    return best


class ConvergenceQuotes:
    """Throttled, idempotent storage for convergence book snapshots."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._ready = False
        self._attempts: dict[tuple[str, str], datetime] = {}
        # Reference levels are static per contract; cache the last valid
        # one seen so a snapshot whose fetch lost the metadata still
        # carries the level an earlier snapshot of the same contract
        # resolved. Bounded by the same 20-minute horizon as _attempts.
        self._reference_levels: dict[
            tuple[str, str], tuple[float, datetime]] = {}

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.store.connect() as conn:
                conn.executescript(_SCHEMA)
                _ensure_reference_level_column(conn, self.store)
            self._ready = True

    def _reference_for(self, venue: str, contract_id: str,
                       value, at: datetime) -> float | None:
        """The contract's reference level: fresh value, else the cache."""
        clock = at.astimezone(UTC)
        self._reference_levels = {
            k: v for k, v in self._reference_levels.items()
            if clock - v[1] < timedelta(minutes=20)}
        key = (venue, contract_id)
        level = _valid_reference(value)
        if level is not None:
            self._reference_levels[key] = (level, clock)
            return level
        cached = self._reference_levels.get(key)
        return cached[0] if cached is not None else None

    def due(self, venue: str, contract_id: str, now: datetime, *,
            cooldown_seconds: int = 30) -> bool:
        """Per-contract throttle, mirroring the XMarket inputs pattern."""
        clock = now.astimezone(UTC)
        self._attempts = {k: v for k, v in self._attempts.items()
                          if clock - v < timedelta(minutes=20)}
        key = (venue, contract_id)
        if key in self._attempts and clock - self._attempts[key] < timedelta(
                seconds=cooldown_seconds):
            return False
        self._attempts[key] = clock
        return True

    def record(self, row: dict) -> bool:
        self.initialize()
        with self._lock, self.store.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO rival_convergence_book_snapshots
                   (venue, asset, contract_id, window_open, window_close,
                    source_at, received_at, best_bid, best_ask, bid_size,
                    ask_size, avg_bid, avg_ask, fee_rate, reference_level,
                    book_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (row["venue"], row["asset"], row["contract_id"],
                 _iso(row["window_open"]), _iso(row["window_close"]),
                 _iso(row["source_at"]), _iso(row["received_at"]),
                 row.get("best_bid"), row.get("best_ask"),
                 row.get("bid_size"), row.get("ask_size"),
                 row.get("avg_bid"), row.get("avg_ask"),
                 row.get("fee_rate"), row.get("reference_level"),
                 row["book_sha256"]),
            )
            return cursor.rowcount > 0

    def record_polymarket(self, *, asset: str, slug: str, window_open: datetime,
                          window_close: datetime, fee_rate: float | None,
                          book, received_at: datetime,
                          reference_level=None) -> bool:
        snap = clob_snapshot(book, received_at=received_at)
        if snap is None:
            return False
        return self.record({
            "venue": "polymarket", "asset": asset, "contract_id": slug,
            "window_open": window_open, "window_close": window_close,
            "received_at": received_at, "fee_rate": fee_rate,
            "reference_level": self._reference_for(
                "polymarket", slug, reference_level, received_at),
            **snap,
        })

    def record_kalshi(self, *, asset: str, ticker: str, window_open: datetime,
                      window_close: datetime, payload,
                      received_at: datetime,
                      reference_level=None) -> bool:
        snap = kalshi_snapshot(payload, received_at=received_at)
        if snap is None:
            return False
        return self.record({
            "venue": "kalshi", "asset": asset, "contract_id": ticker,
            "window_open": window_open, "window_close": window_close,
            "received_at": received_at, "fee_rate": None,
            "reference_level": self._reference_for(
                "kalshi", ticker, reference_level, received_at),
            **snap,
        })

    def record_reference(self, *, venue: str, asset: str, contract_id: str,
                         window_open: datetime, window_close: datetime,
                         reference_level, resolved_at: datetime) -> bool:
        """Store a venue-published reference level for one contract.

        A separate record, not a snapshot: Polymarket publishes its
        price-to-beat only after the window closes, when no book of
        that contract can still pass snapshot freshness, so the level
        cannot honestly ride a book row -- and existing snapshot rows
        are never modified. The first published value wins (the level
        is static per contract); an unusable value stores nothing.
        """
        level = _valid_reference(reference_level)
        if level is None:
            return False
        self.initialize()
        with self._lock, self.store.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO rival_convergence_reference_levels
                   (venue, asset, contract_id, window_open, window_close,
                    reference_level, resolved_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (venue, asset, contract_id, _iso(window_open),
                 _iso(window_close), level, _iso(resolved_at)),
            )
            return cursor.rowcount > 0

    def reference_level_for(self, venue: str, contract_id: str) -> float | None:
        """The stored reference level for one contract, or None."""
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT reference_level FROM rival_convergence_reference_levels
                   WHERE venue=? AND contract_id=?""",
                (venue, contract_id),
            ).fetchone()
        return _valid_reference(row["reference_level"]) if row else None

    def snapshots(self, *, venue: str, contract_id: str) -> list[dict]:
        """Stored snapshots for one contract, in source-time order."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_convergence_book_snapshots
                   WHERE venue=? AND contract_id=? ORDER BY source_at""",
                (venue, contract_id),
            ).fetchall()
        return [dict(row) for row in rows]
