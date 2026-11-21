"""Kalshi threshold/barrier market snapshots for the N1 forward collection.

The Kalshi side of the options-implied pre-test (design + forward
amendment, 2026-11-19). The Polymarket arm records live (Gamma) and
the Deribit surface records live, but the exploratory Kalshi arm was
left to pull from Kalshi's own free historical candlesticks archive at
evaluation time -- and that archive has a moving cutoff (step 0 read
it as 2026-09-19), so every week without a forward recorder is a week
the archive may silently stop covering. This recorder removes that
risk: the open Kalshi BTC/ETH threshold markets are snapshotted hourly
from the venue's public REST API, which serves series, markets and
prices with no authentication (verified from the sandbox 2026-11-20).

Discovery is by series, verified against the live API on 2026-11-20
(tickers in ``SERIES``): the hourly above/below pair (KXBTCD/KXETHD),
the hourly range pair (KXBTC/KXETH, whose events mix between-brackets
with greater/less tails), the annual range pair (KXBTCY/KXETHY), and
the one-touch max/min series at monthly and annual horizons. The
legacy daily series (BTCD, ETHD, BTC, ETH) and the weekly/daily
one-touch series (KXBTCMAXW, KXBTCMAXD, KXBTCMAXM) currently list no
markets at all on the venue, so they are not in the table; if Kalshi
revives one it joins by amendment, never silently. Each market is
classified from the venue's own ``strike_type`` -- between is a range
bracket, greater/less are above/below in the close-based families and
reach/dip in the one-touch families -- and its terms persist verbatim
(title, subtitle, rules) alongside the parsed strike bounds. A market
whose terms cannot be parsed is skipped and counted, never guessed.
Kalshi's public API carries no per-market source clock for these
markets, so the receipt time is the snapshot time -- the same
convention the convergence collector uses for Kalshi books.

Collection only: no wedge is computed here, no model reads this table,
no order is placed. The Kalshi arm stays exploratory and non-verdict
under the design; the single future consumer is the read-only N1
evaluator, run once when the amendment's frozen floors are met.
"""
from __future__ import annotations

import asyncio
import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ASSETS = ("BTC", "ETH")


@dataclass(frozen=True)
class SeriesSpec:
    """One in-scope Kalshi series, as verified live on 2026-11-20."""

    asset: str
    family: str  # "above" | "range" | "touch"
    series_ticker: str
    horizon: str  # the series' own recurrence: hourly | monthly | annual


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("BTC", "above", "KXBTCD", "hourly"),
    SeriesSpec("ETH", "above", "KXETHD", "hourly"),
    SeriesSpec("BTC", "range", "KXBTC", "hourly"),
    SeriesSpec("ETH", "range", "KXETH", "hourly"),
    SeriesSpec("BTC", "range", "KXBTCY", "annual"),
    SeriesSpec("ETH", "range", "KXETHY", "annual"),
    SeriesSpec("BTC", "touch", "KXBTCMAXMON", "monthly"),
    SeriesSpec("BTC", "touch", "KXBTCMINMON", "monthly"),
    SeriesSpec("ETH", "touch", "KXETHMAXMON", "monthly"),
    SeriesSpec("ETH", "touch", "KXETHMINMON", "monthly"),
    SeriesSpec("BTC", "touch", "KXBTCMAXY", "annual"),
    SeriesSpec("BTC", "touch", "KXBTCMINY", "annual"),
    SeriesSpec("ETH", "touch", "KXETHMAXY", "annual"),
    SeriesSpec("ETH", "touch", "KXETHMINY", "annual"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_kalshi_threshold_snapshots (
    snapshot_at TEXT NOT NULL,
    asset TEXT NOT NULL, contract_type TEXT NOT NULL, horizon TEXT NOT NULL,
    series_ticker TEXT NOT NULL, event_ticker TEXT,
    market_ticker TEXT NOT NULL,
    title TEXT, subtitle TEXT,
    strike_type TEXT, strike_low DOUBLE PRECISION,
    strike_high DOUBLE PRECISION,
    yes_bid DOUBLE PRECISION, yes_ask DOUBLE PRECISION,
    no_bid DOUBLE PRECISION, no_ask DOUBLE PRECISION,
    last_price DOUBLE PRECISION,
    status TEXT, open_time TEXT, close_time TEXT,
    expected_expiration_time TEXT,
    rules_primary TEXT, rules_secondary TEXT,
    PRIMARY KEY (market_ticker, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_kalshi_threshold_series
    ON rival_kalshi_threshold_snapshots (asset, contract_type, close_time);
CREATE INDEX IF NOT EXISTS idx_kalshi_threshold_snapshot
    ON rival_kalshi_threshold_snapshots (snapshot_at);
"""


def _num(value) -> float | None:
    """A finite numeric field, or None (bools and strings are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    return level if math.isfinite(level) else None


def _dollars(value) -> float | None:
    """A Kalshi fixed-point dollar price (``"0.5600"``), or None.

    The public API states prices as dollar strings in 0..1; a numeric
    value is already in dollars. Anything unparseable or outside the
    probability range is absent evidence, never a repaired price.
    """
    if isinstance(value, str):
        try:
            level = float(value.strip())
        except ValueError:
            return None
    else:
        level = _num(value)
    if level is None or not 0.0 <= level <= 1.0:
        return None
    return level


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("threshold snapshot clock requires timezone")
    return value.astimezone(UTC).isoformat()


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _terms(spec: SeriesSpec, market: dict) -> tuple[str, float | None, float | None] | None:
    """(contract_type, strike_low, strike_high) from the venue's strike_type.

    The venue classifies its own markets: ``between`` is a bracket
    carrying both bounds, ``greater``/``less`` carry the floor/cap
    alone. In the one-touch families those read as reach/dip; in the
    close-based families as above/below. A market missing the bound
    its type requires -- or carrying a type the venue has not used
    here -- is not evidence and yields None.
    """
    strike_type = market.get("strike_type")
    floor = _num(market.get("floor_strike"))
    cap = _num(market.get("cap_strike"))
    if strike_type == "between":
        if floor is None or cap is None:
            return None
        return ("range", floor, cap)
    if strike_type == "greater":
        if floor is None:
            return None
        return ("reach" if spec.family == "touch" else "above", floor, None)
    if strike_type == "less":
        if cap is None:
            return None
        return ("dip" if spec.family == "touch" else "below", None, cap)
    return None


def market_row(spec: SeriesSpec, market: dict) -> dict | None:
    """One snapshot row from a Kalshi market, or None when it is not evidence.

    A row needs a ticker (its identity), parseable terms (its
    contract) and a close time (its expiry). Prices and rules persist
    as stated, NULL when absent.
    """
    if not isinstance(market, dict):
        return None
    ticker = market.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        return None
    terms = _terms(spec, market)
    if terms is None:
        return None
    contract_type, strike_low, strike_high = terms
    close_time = _text(market.get("close_time"))
    if close_time is None:
        return None
    return {
        "asset": spec.asset,
        "contract_type": contract_type,
        "horizon": spec.horizon,
        "series_ticker": spec.series_ticker,
        "event_ticker": _text(market.get("event_ticker")),
        "market_ticker": ticker,
        "title": _text(market.get("title")),
        "subtitle": _text(market.get("yes_sub_title")) or _text(market.get("subtitle")),
        "strike_type": _text(market.get("strike_type")),
        "strike_low": strike_low,
        "strike_high": strike_high,
        "yes_bid": _dollars(market.get("yes_bid_dollars")),
        "yes_ask": _dollars(market.get("yes_ask_dollars")),
        "no_bid": _dollars(market.get("no_bid_dollars")),
        "no_ask": _dollars(market.get("no_ask_dollars")),
        "last_price": _dollars(market.get("last_price_dollars")),
        "status": _text(market.get("status")),
        "open_time": _text(market.get("open_time")),
        "close_time": close_time,
        "expected_expiration_time": _text(market.get("expected_expiration_time")),
        "rules_primary": _text(market.get("rules_primary")),
        "rules_secondary": _text(market.get("rules_secondary")),
    }


class KalshiThresholdsRecorder:
    """Idempotent storage + public-API capture for Kalshi threshold snapshots.

    ``capture`` is the bot loop's entry point: each series is fetched
    and stored independently, so a Kalshi failure for one series
    degrades that family for the hour and nothing else. The receipt
    counts rows per series; it carries no payload data.
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

    def record_markets(self, spec: SeriesSpec, markets: list, *,
                       snapshot_at: datetime) -> tuple[int, int]:
        """Persist one series' markets; returns (written, skipped)."""
        self.initialize()
        if not isinstance(markets, list):
            return 0, 0
        stamp = _iso(snapshot_at)
        pending = []
        skipped = 0
        for market in markets:
            row = market_row(spec, market)
            if row is None:
                skipped += 1
                continue
            pending.append(
                (stamp, row["asset"], row["contract_type"], row["horizon"],
                 row["series_ticker"], row["event_ticker"],
                 row["market_ticker"], row["title"], row["subtitle"],
                 row["strike_type"], row["strike_low"], row["strike_high"],
                 row["yes_bid"], row["yes_ask"], row["no_bid"],
                 row["no_ask"], row["last_price"], row["status"],
                 row["open_time"], row["close_time"],
                 row["expected_expiration_time"], row["rules_primary"],
                 row["rules_secondary"]))
        written = 0
        with self._lock, self.store.connect() as conn:
            # 100-row multi-value inserts (the market_store pattern) replace
            # ~1,270 per-row round trips per hourly snapshot. RETURNING
            # counts only rows that actually landed — the same count the
            # per-row rowcount produced under ON CONFLICT DO NOTHING.
            for offset in range(0, len(pending), 100):
                chunk = pending[offset:offset + 100]
                placeholders = ", ".join(
                    "(" + ",".join("?" for _ in range(23)) + ")"
                    for _ in chunk
                )
                params = tuple(item for values in chunk for item in values)
                landed = conn.execute(
                    "INSERT INTO rival_kalshi_threshold_snapshots "
                    "(snapshot_at, asset, contract_type, horizon, "
                    "series_ticker, event_ticker, market_ticker, title, "
                    "subtitle, strike_type, strike_low, strike_high, "
                    "yes_bid, yes_ask, no_bid, no_ask, last_price, "
                    "status, open_time, close_time, "
                    "expected_expiration_time, rules_primary, "
                    "rules_secondary) VALUES "
                    + placeholders
                    + " ON CONFLICT DO NOTHING RETURNING market_ticker",
                    params,
                ).fetchall()
                written += len(landed)
        return written, skipped

    async def _fetch_series(self, client: httpx.AsyncClient,
                            spec: SeriesSpec) -> list[dict]:
        """Open markets for one series, cursor-paged to a bounded depth."""
        markets: list[dict] = []
        cursor: str | None = None
        for _page in range(5):
            params = {"series_ticker": spec.series_ticker, "status": "open",
                      "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            response = await client.get(KALSHI_BASE + "/markets", params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("Kalshi markets payload is not an object")
            page = payload.get("markets")
            if not isinstance(page, list):
                raise TypeError("Kalshi markets payload has no markets list")
            markets.extend(m for m in page if isinstance(m, dict))
            cursor = payload.get("cursor")
            if not cursor or not page:
                break
        return markets

    async def capture_series(self, client: httpx.AsyncClient,
                             spec: SeriesSpec, *, now: datetime) -> dict:
        """Fetch + store one series; never raises for venue faults."""
        try:
            markets = await self._fetch_series(client, spec)
            written, skipped = await asyncio.to_thread(
                self.record_markets, spec, markets, snapshot_at=now)
            return {"series": spec.series_ticker, "markets": len(markets),
                    "rows": written, "skipped": skipped}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one series' outage is local
            return {"series": spec.series_ticker, "error": type(exc).__name__}

    async def capture(self, client: httpx.AsyncClient, *,
                      now: datetime) -> dict:
        """One hourly cycle over every in-scope series, failure-isolated."""
        receipt = {}
        for spec in SERIES:
            result = await self.capture_series(client, spec, now=now)
            receipt[spec.series_ticker] = result
        return receipt

    def snapshots(self, *, asset: str | None = None,
                  contract_type: str | None = None) -> list[dict]:
        """Stored rows, optionally filtered, in snapshot order."""
        self.initialize()
        query = "SELECT * FROM rival_kalshi_threshold_snapshots"
        clauses = []
        params: list = []
        if asset is not None:
            clauses.append("asset=?")
            params.append(asset)
        if contract_type is not None:
            clauses.append("contract_type=?")
            params.append(contract_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY snapshot_at, market_ticker"
        with self.store.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]
