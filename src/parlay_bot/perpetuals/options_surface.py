"""Deribit options-surface snapshots for the N1 forward collection.

The options-implied pre-test (design 2026-11-19) prices Polymarket
threshold/barrier contracts off the Deribit options surface: the surface
is the sharp reference, and the test reads a risk-neutral distribution
off an hourly snapshot of it. Step 0 of that design verified the gating
fact: Deribit's public API serves only the LIVE surface, and no free
historical archive of it exists -- the archives with the needed fields
are paid. Felix chose the free forward route (amendment 2026-11-19):
record the live surface hourly from deploy and let the sample accrue,
exactly as the settlement-index recorders accrue theirs. If it is not
recorded live, it does not exist.

This module is that recorder. One public call per currency
(``public/get_book_summary_by_currency``) returns every listed option's
mark price, mark IV and underlying in a single response, so an hourly
snapshot of the full BTC+ETH surface costs two requests (plus one
best-effort index-price call per currency). Rows persist the raw venue
fields -- instrument, strike, expiry, type, mark, IV, bid/ask,
underlying, index -- raw enough for the later evaluator to reconstruct
the implied distribution per snapshot (the design's Kou calibration
runs per hourly surface). Malformed entries are skipped and counted,
never repaired; a currency whose fetch fails is absent for that hour,
never carried forward.

Collection only: no wedge is computed here, no probability is fitted,
no model reads this table, no order is placed. The single future
consumer is the read-only N1 evaluator, run once when the amendment's
frozen floors are met.
"""
from __future__ import annotations

import asyncio
import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time

import httpx

DERIBIT_BASE = "https://www.deribit.com/api/v2/public/"
CURRENCIES = ("BTC", "ETH")
# Rows per fetchmany chunk when streaming evaluator-side reads (the #727
# pattern): identical ORDER BY order and contents to fetchall(), without
# materializing a whole snapshot table before the first row is processed.
FETCH_CHUNK = 2000
# Deribit options expire at 08:00 UTC on their expiry date.
_EXPIRY_TIME = time(8, 0)
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_INSTRUMENT = re.compile(
    r"^([A-Z]{2,10})-(\d{1,2})([A-Z]{3})(\d{2})-([0-9]+(?:\.[0-9]+)?)-([CP])$"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_options_surface_snapshots (
    snapshot_at TEXT NOT NULL,
    currency TEXT NOT NULL, instrument_name TEXT NOT NULL,
    expiry_at TEXT NOT NULL, strike DOUBLE PRECISION NOT NULL,
    option_type TEXT NOT NULL,
    mark_price DOUBLE PRECISION NOT NULL, mark_iv DOUBLE PRECISION NOT NULL,
    bid_price DOUBLE PRECISION, ask_price DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION, index_price DOUBLE PRECISION,
    PRIMARY KEY (snapshot_at, instrument_name)
);
CREATE INDEX IF NOT EXISTS idx_options_surface_instrument
    ON rival_options_surface_snapshots (currency, expiry_at, strike);
"""


@dataclass(frozen=True)
class InstrumentTerms:
    """The contract terms encoded in a Deribit instrument name."""

    currency: str
    expiry_at: datetime
    strike: float
    option_type: str  # "call" | "put"


def parse_instrument_name(name) -> InstrumentTerms | None:
    """Terms from a name like ``BTC-20NOV26-64000-P``, or None.

    The name is the venue's own encoding (currency, expiry date, strike,
    C/P); anything that does not match it exactly is not evidence and
    yields no terms rather than a guess.
    """
    if not isinstance(name, str):
        return None
    match = _INSTRUMENT.match(name.strip())
    if match is None:
        return None
    currency, day, month_name, year, strike, flag = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        expiry = datetime(
            2000 + int(year), month, int(day), _EXPIRY_TIME.hour,
            _EXPIRY_TIME.minute, tzinfo=UTC)
        level = float(strike)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(level) or level <= 0:
        return None
    return InstrumentTerms(
        currency=currency, expiry_at=expiry, strike=level,
        option_type="call" if flag == "C" else "put")


def _num(value) -> float | None:
    """A finite numeric field, or None (bools and strings are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    return level if math.isfinite(level) else None


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("surface snapshot clock requires timezone")
    return value.astimezone(UTC).isoformat()


def surface_rows(payload, *, index_price=None) -> tuple[list[dict], int]:
    """Row dicts from one get_book_summary_by_currency envelope.

    Returns (rows, skipped). An entry lands only if its instrument name
    parses and its mark price and mark IV are finite numbers -- those
    are the fields the implied distribution is reconstructed from.
    Bid/ask and the underlying are persisted when the venue states them
    and stay NULL when it does not; their absence does not void the mark
    pair. The envelope itself must carry a result list: anything else
    raises ValueError, which the caller treats as a failed fetch.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        raise TypeError("Deribit book summary envelope lacks a result list")
    index = _num(index_price)
    rows: list[dict] = []
    skipped = 0
    for entry in payload["result"]:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        terms = parse_instrument_name(entry.get("instrument_name"))
        mark_price = _num(entry.get("mark_price"))
        mark_iv = _num(entry.get("mark_iv"))
        if terms is None or mark_price is None or mark_iv is None:
            skipped += 1
            continue
        rows.append({
            "currency": terms.currency,
            "instrument_name": entry["instrument_name"],
            "expiry_at": terms.expiry_at,
            "strike": terms.strike,
            "option_type": terms.option_type,
            "mark_price": mark_price,
            "mark_iv": mark_iv,
            "bid_price": _num(entry.get("bid_price")),
            "ask_price": _num(entry.get("ask_price")),
            "underlying_price": _num(entry.get("underlying_price")),
            "index_price": index,
        })
    return rows, skipped


class OptionsSurfaceRecorder:
    """Idempotent storage + public-API capture for surface snapshots.

    ``capture`` is the bot loop's entry point: each currency is fetched
    and stored independently, so a Deribit outage for one currency (or
    for the index-price side call) degrades that slice of the hour and
    nothing else. The receipt counts rows and failures; it carries no
    payload data.
    """

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.store.connect() as conn:
                conn.executescript(_SCHEMA)
            self._ready = True

    def record_snapshot(self, rows: list[dict], *, snapshot_at: datetime) -> int:
        """Persist one snapshot's rows; returns the number written."""
        self.initialize()
        stamp = _iso(snapshot_at)
        pending = [
            (stamp, row["currency"], row["instrument_name"],
             _iso(row["expiry_at"]), row["strike"], row["option_type"],
             row["mark_price"], row["mark_iv"], row.get("bid_price"),
             row.get("ask_price"), row.get("underlying_price"),
             row.get("index_price"))
            for row in rows
        ]
        written = 0
        with self._lock, self.store.connect() as conn:
            # 100-row multi-value inserts (the market_store pattern) replace
            # ~1,800 per-row round trips per hourly snapshot. RETURNING
            # counts only rows that actually landed — the same count the
            # per-row rowcount produced under ON CONFLICT DO NOTHING.
            for offset in range(0, len(pending), 100):
                chunk = pending[offset:offset + 100]
                placeholders = ", ".join(
                    "(" + ",".join("?" for _ in range(12)) + ")"
                    for _ in chunk
                )
                params = tuple(item for values in chunk for item in values)
                landed = conn.execute(
                    "INSERT INTO rival_options_surface_snapshots "
                    "(snapshot_at, currency, instrument_name, expiry_at, "
                    "strike, option_type, mark_price, mark_iv, bid_price, "
                    "ask_price, underlying_price, index_price) VALUES "
                    + placeholders
                    + " ON CONFLICT DO NOTHING RETURNING instrument_name",
                    params,
                ).fetchall()
                written += len(landed)
        return written

    async def _fetch(self, client: httpx.AsyncClient, path: str,
                     params: dict) -> object:
        response = await client.get(DERIBIT_BASE + path, params=params)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise ValueError(f"Deribit returned an error for {path}")
        return payload

    async def capture_currency(self, client: httpx.AsyncClient,
                               currency: str, *, now: datetime) -> dict:
        """Fetch + store one currency's surface; never raises for venue faults."""
        try:
            payload = await self._fetch(
                client, "get_book_summary_by_currency",
                {"currency": currency, "kind": "option"})
            index_price = None
            try:
                index_payload = await self._fetch(
                    client, "get_index_price",
                    {"index_name": f"{currency.lower()}_usd"})
                if isinstance(index_payload, dict):
                    result = index_payload.get("result")
                    if isinstance(result, dict):
                        index_price = result.get("index_price")
            except Exception:  # noqa: BLE001 - the index print is best-effort
                index_price = None
            rows, skipped = surface_rows(payload, index_price=index_price)
            written = await asyncio.to_thread(
                self.record_snapshot, rows, snapshot_at=now)
            return {"currency": currency, "rows": written, "skipped": skipped}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one currency's outage is local
            return {"currency": currency, "error": type(exc).__name__}

    async def capture(self, client: httpx.AsyncClient, *,
                      now: datetime) -> dict:
        """One hourly cycle over both currencies, each failure-isolated."""
        receipt = {}
        for currency in CURRENCIES:
            receipt[currency] = await self.capture_currency(
                client, currency, now=now)
        return receipt

    def snapshots(self, *, currency: str,
                  snapshot_at: datetime | None = None) -> list[dict]:
        """Stored rows for one currency, in snapshot/strike order."""
        self.initialize()
        rows: list[dict] = []
        with self.store.connect() as conn:
            if snapshot_at is None:
                cursor = conn.execute(
                    """SELECT * FROM rival_options_surface_snapshots
                       WHERE currency=? ORDER BY snapshot_at, strike""",
                    (currency,),
                )
            else:
                cursor = conn.execute(
                    """SELECT * FROM rival_options_surface_snapshots
                       WHERE currency=? AND snapshot_at=?
                       ORDER BY strike""",
                    (currency, _iso(snapshot_at)),
                )
            while True:
                chunk = cursor.fetchmany(FETCH_CHUNK)
                if not chunk:
                    break
                rows.extend(dict(row) for row in chunk)
        return rows

    def snapshot_times(self, *, currency: str) -> list[str]:
        """Distinct snapshot stamps recorded for one currency, in order."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT snapshot_at FROM
                   rival_options_surface_snapshots
                   WHERE currency=? ORDER BY snapshot_at""",
                (currency,),
            ).fetchall()
        return [row["snapshot_at"] for row in rows]
