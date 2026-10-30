"""Read-only, timestamp-verified Binance USD-M top-of-book evidence.

Best bid/ask is NOT a guaranteed executable fill. Provider-blocked regions
fail closed; REST calls never run from Discord's gateway/event-loop polling.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
import httpx

if TYPE_CHECKING:
    from parlay_bot.perpetuals.stream_quotes import StreamingBookHub

ASSETS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
MAX_QUOTE_AGE = timedelta(seconds=10)
CACHE_SECONDS = 3.0
BLOCK_SECONDS = 3600.0


class QuoteUnavailable(RuntimeError):
    """No verified executable top-of-book evidence, never use candle instead."""


@dataclass(frozen=True)
class BookQuote:
    asset: str
    venue: str
    symbol: str
    bid: float
    bid_quantity: float
    ask: float
    ask_quantity: float
    exchange_at: datetime
    received_at: datetime

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 10000

    def validate(self, *, now: datetime) -> None:
        if self.asset not in ASSETS or self.symbol != ASSETS[self.asset]:
            raise QuoteUnavailable("Wrong exchange instrument.")
        if self.exchange_at.utcoffset() is None or self.received_at.utcoffset() is None:
            raise QuoteUnavailable("Missing exchange timestamp.")
        if any(not math.isfinite(v) or v <= 0 for v in (
            self.bid, self.ask, self.bid_quantity, self.ask_quantity,
        )):
            raise QuoteUnavailable("Invalid bid/ask or top-of-book liquidity.")
        if self.ask <= self.bid or self.spread_bps > 200:
            raise QuoteUnavailable("Crossed or implausibly wide order book.")
        if not timedelta(0) <= now - self.exchange_at <= MAX_QUOTE_AGE:
            raise QuoteUnavailable("Exchange quote is stale or from the future.")
        if not timedelta(0) <= now - self.received_at <= MAX_QUOTE_AGE:
            raise QuoteUnavailable("Local quote receipt is stale or from the future.")


def parse_book_quote(
    payload: Any, *, asset: str, received_at: datetime,
) -> BookQuote:
    if asset not in ASSETS or not isinstance(payload, dict):
        raise QuoteUnavailable("Unsupported asset or malformed exchange response.")
    if payload.get("symbol") != ASSETS[asset]:
        raise QuoteUnavailable("Exchange returned another contract.")
    try:
        timestamp = payload["time"]
        if type(timestamp) is not int or timestamp <= 0:
            raise ValueError("invalid timestamp")
        quote = BookQuote(
            asset=asset, venue="binance_usdm",
            symbol=ASSETS[asset], bid=float(payload["bidPrice"]),
            bid_quantity=float(payload["bidQty"]),
            ask=float(payload["askPrice"]),
            ask_quantity=float(payload["askQty"]),
            exchange_at=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
            received_at=received_at,
        )
        quote.validate(now=received_at)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise QuoteUnavailable("Invalid top-of-book response.") from exc
    return quote


@dataclass(frozen=True)
class PerpReferenceBook:
    """Independent Hyperliquid USD perp book, never a Binance fill quote."""

    asset: str
    venue: str
    bid: float
    ask: float
    bid_quantity: float
    ask_quantity: float
    exchange_at: datetime
    received_at: datetime

    def validate(self, *, now: datetime) -> None:
        if self.asset not in ASSETS or self.venue != "hyperliquid":
            raise QuoteUnavailable("Unverified perp reference identity.")
        if self.exchange_at.utcoffset() is None or self.received_at.utcoffset() is None:
            raise QuoteUnavailable("Missing perp reference clock.")
        prices = (self.bid, self.ask, self.bid_quantity, self.ask_quantity)
        if any(not math.isfinite(v) or v <= 0 for v in prices):
            raise QuoteUnavailable("Invalid perp reference depth.")
        if self.ask <= self.bid or (self.ask - self.bid) / self.ask > 0.02:
            raise QuoteUnavailable("Crossed or implausibly wide perp reference.")
        if not timedelta(0) <= now - self.exchange_at <= MAX_QUOTE_AGE:
            raise QuoteUnavailable("Perp reference is stale.")
        if not timedelta(0) <= now - self.received_at <= MAX_QUOTE_AGE:
            raise QuoteUnavailable("Perp reference receipt is stale.")


def parse_hyperliquid_reference(
    payload: Any, *, asset: str, received_at: datetime,
) -> PerpReferenceBook:
    """Official public info/l2Book. Native USD perp price, NOT USDT execution."""
    if asset not in ASSETS or not isinstance(payload, dict):
        raise QuoteUnavailable("Unsupported perp reference.")
    if payload.get("coin") != asset:
        raise QuoteUnavailable("Wrong perp reference instrument.")
    try:
        milliseconds = payload["time"]
        if type(milliseconds) is not int or milliseconds <= 0:
            raise ValueError("missing exchange clock")
        levels = payload["levels"]
        if (not isinstance(levels, list) or len(levels) != 2
                or not all(isinstance(side, list) and side for side in levels)):
            raise ValueError("empty reference book")
        bid, ask = levels[0][0], levels[1][0]
        reference = PerpReferenceBook(
            asset=asset, venue="hyperliquid",
            bid=float(bid["px"]), ask=float(ask["px"]),
            bid_quantity=float(bid["sz"]), ask_quantity=float(ask["sz"]),
            exchange_at=datetime.fromtimestamp(milliseconds / 1000, tz=UTC),
            received_at=received_at,
        )
        reference.validate(now=received_at)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        raise QuoteUnavailable("Invalid current perp reference book.") from exc
    return reference


# Verified live top-of-book sources beyond Hyperliquid/Binance. Symbols are
# the venues' own USDT-perp contracts, already used by the candle collectors
# (OKX "{asset}-USDT-SWAP", Bitget "{asset}USDT" USDT-FUTURES). A forecast's
# book must still come from its OWN venue; these adapters widen which venues
# can supply one, they never substitute another venue's price.
VENUE_BOOK_SYMBOLS: dict[str, dict[str, str]] = {
    "okx": {asset: f"{asset}-USDT-SWAP" for asset in ASSETS},
    "bitget": {asset: f"{asset}USDT" for asset in ASSETS},
}
VENUE_BOOK_BASE_URLS: dict[str, str] = {
    "okx": "https://www.okx.com",
    "bitget": "https://api.bitget.com",
}


@dataclass(frozen=True)
class VenueBookQuote:
    """Top of book from a venue other than Binance/Hyperliquid.

    Same validation bounds as BookQuote (spread, freshness, quantities);
    the symbol check is venue-aware instead of Binance-hardcoded.
    """

    asset: str
    venue: str
    symbol: str
    bid: float
    bid_quantity: float
    ask: float
    ask_quantity: float
    exchange_at: datetime
    received_at: datetime

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 10000

    def validate(self, *, now: datetime) -> None:
        expected = VENUE_BOOK_SYMBOLS.get(self.venue, {}).get(self.asset)
        if expected is None or self.symbol != expected:
            raise QuoteUnavailable("Wrong exchange instrument.")
        if self.exchange_at.utcoffset() is None or self.received_at.utcoffset() is None:
            raise QuoteUnavailable("Missing exchange timestamp.")
        if any(not math.isfinite(v) or v <= 0 for v in (
            self.bid, self.ask, self.bid_quantity, self.ask_quantity,
        )):
            raise QuoteUnavailable("Invalid bid/ask or top-of-book liquidity.")
        if self.ask <= self.bid or self.spread_bps > 200:
            raise QuoteUnavailable("Crossed or implausibly wide order book.")
        if not timedelta(0) <= now - self.exchange_at <= MAX_QUOTE_AGE:
            raise QuoteUnavailable("Exchange quote is stale or from the future.")
        if not timedelta(0) <= now - self.received_at <= MAX_QUOTE_AGE:
            raise QuoteUnavailable("Local quote receipt is stale or from the future.")


def _parse_venue_ticker(
    payload: Any, *, asset: str, venue: str, received_at: datetime,
) -> VenueBookQuote:
    if asset not in ASSETS or venue not in VENUE_BOOK_SYMBOLS:
        raise QuoteUnavailable("Unsupported asset or venue.")
    if not isinstance(payload, dict):
        raise QuoteUnavailable("Malformed exchange response.")
    if venue == "okx" and payload.get("code") != "0":
        raise QuoteUnavailable("OKX ticker request rejected.")
    if venue == "bitget" and payload.get("code") != "00000":
        raise QuoteUnavailable("Bitget ticker request rejected.")
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise QuoteUnavailable("Empty ticker response.")
    row = data[0]
    symbol_key = "instId" if venue == "okx" else "symbol"
    try:
        milliseconds = row["ts"]
        if isinstance(milliseconds, str):
            milliseconds = int(milliseconds)
        if type(milliseconds) is not int or milliseconds <= 0:
            raise ValueError("invalid timestamp")
        quote = VenueBookQuote(
            asset=asset, venue=venue, symbol=str(row[symbol_key]),
            bid=float(row["bidPx"]), bid_quantity=float(row["bidSz"]),
            ask=float(row["askPx"]), ask_quantity=float(row["askSz"]),
            exchange_at=datetime.fromtimestamp(milliseconds / 1000, tz=UTC),
            received_at=received_at,
        )
        quote.validate(now=received_at)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise QuoteUnavailable("Invalid top-of-book response.") from exc
    return quote


def parse_okx_ticker(
    payload: Any, *, asset: str, received_at: datetime,
) -> VenueBookQuote:
    """OKX /api/v5/market/ticker row for a USDT-SWAP contract."""
    return _parse_venue_ticker(payload, asset=asset, venue="okx", received_at=received_at)


def parse_bitget_ticker(
    payload: Any, *, asset: str, received_at: datetime,
) -> VenueBookQuote:
    """Bitget /api/v2/mix/market/ticker row for a USDT-FUTURES contract."""
    return _parse_venue_ticker(payload, asset=asset, venue="bitget", received_at=received_at)


class PublicBestQuoteClient:
    """Bounded shared REST snapshot; no API keys, wallet or order endpoint."""

    def __init__(
        self, *, client: httpx.AsyncClient | None = None,
        base_url: str = "https://fapi.binance.com",
        reference_client: httpx.AsyncClient | None = None,
        venue_clients: dict[str, httpx.AsyncClient] | None = None,
    ) -> None:
        self._owns = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(5.0),
        )
        self._cache: dict[str, tuple[float, BookQuote]] = {}
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        # Independent read-only fallback, used ONLY for display. Never feed
        # these USD perp prices into Binance-USDT paper/profit settlement.
        self._owns_reference = reference_client is None
        self._reference_client = reference_client
        self._reference_cache: dict[str, tuple[float, PerpReferenceBook]] = {}
        self._reference_blocked_until = 0.0
        self._reference_lock = asyncio.Lock()
        self.stream_hub: StreamingBookHub | None = None
        # Additional venue books (OKX, Bitget). Clients passed in are owned
        # by the caller, like the Binance/reference clients above.
        self._venue_clients: dict[str, httpx.AsyncClient] = dict(venue_clients or {})
        self._owns_venue_clients = set(VENUE_BOOK_SYMBOLS) - set(self._venue_clients)
        self._venue_cache: dict[tuple[str, str], tuple[float, VenueBookQuote]] = {}
        self._venue_blocked_until: dict[str, float] = {}
        self._venue_locks: dict[str, asyncio.Lock] = {
            venue: asyncio.Lock() for venue in VENUE_BOOK_SYMBOLS
        }

    async def aclose(self) -> None:
        if self._owns:
            await self.client.aclose()
        if self._owns_reference and self._reference_client is not None:
            await self._reference_client.aclose()
        for venue in self._owns_venue_clients:
            venue_client = self._venue_clients.get(venue)
            if venue_client is not None:
                await venue_client.aclose()

    async def get(self, asset: str) -> BookQuote:
        if asset not in ASSETS:
            raise QuoteUnavailable("Only BTC, ETH, SOL and XRP USDT futures supported.")
        async with self._lock:
            if self.stream_hub is not None:
                streamed = self.stream_hub.snapshot("binance", asset)
                if isinstance(streamed, BookQuote):
                    return streamed
            clock = time.monotonic()
            if clock < self._blocked_until:
                raise QuoteUnavailable("Exchange temporarily blocked or rate limited.")
            cached = self._cache.get(asset)
            now = datetime.now(UTC)
            if cached is not None and clock - cached[0] <= CACHE_SECONDS:
                try:
                    cached[1].validate(now=now)
                    return cached[1]
                except QuoteUnavailable:
                    self._cache.pop(asset, None)
            try:
                resp = await self.client.get(
                    "/fapi/v1/ticker/bookTicker",
                    params={"symbol": ASSETS[asset]},
                )
                if resp.status_code in (401, 403, 451):
                    self._blocked_until = clock + BLOCK_SECONDS
                    raise QuoteUnavailable(
                        "Binance futures quotes unavailable in this region/account."
                    )
                if resp.status_code == 429:
                    self._blocked_until = clock + 60
                    raise QuoteUnavailable("Binance public quote API rate limited.")
                resp.raise_for_status()
                raw = resp.json()
                quote = parse_book_quote(raw, asset=asset, received_at=datetime.now(UTC))
            except QuoteUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise QuoteUnavailable(
                    "Live executable bid/ask could not be verified."
                ) from exc
            self._cache[asset] = (time.monotonic(), quote)
            return quote

    async def get_reference(self, asset: str) -> PerpReferenceBook:
        """A second verified, native USD-perp reference for read-only UI."""
        if asset not in ASSETS:
            raise QuoteUnavailable("Unsupported perpetual market.")
        async with self._reference_lock:
            if self.stream_hub is not None:
                streamed = self.stream_hub.snapshot("hyperliquid", asset)
                if isinstance(streamed, PerpReferenceBook):
                    return streamed
            clock = time.monotonic()
            if clock < self._reference_blocked_until:
                raise QuoteUnavailable("Secondary public exchange unavailable.")
            cached = self._reference_cache.get(asset)
            received = datetime.now(UTC)
            if cached is not None and clock - cached[0] <= CACHE_SECONDS:
                try:
                    cached[1].validate(now=received)
                    return cached[1]
                except QuoteUnavailable:
                    self._reference_cache.pop(asset, None)
            try:
                if self._reference_client is None:
                    self._reference_client = httpx.AsyncClient(
                        base_url="https://api.hyperliquid.xyz",
                        timeout=httpx.Timeout(5.0),
                    )
                response = await self._reference_client.post(
                    "/info", json={"type": "l2Book", "coin": asset},
                )
                if response.status_code in (401, 403, 418, 451):
                    self._reference_blocked_until = clock + BLOCK_SECONDS
                    raise QuoteUnavailable("Secondary public exchange is blocked.")
                if response.status_code == 429:
                    self._reference_blocked_until = clock + 60
                    raise QuoteUnavailable("Secondary public exchange rate limited.")
                response.raise_for_status()
                value = parse_hyperliquid_reference(
                    response.json(), asset=asset, received_at=datetime.now(UTC),
                )
            except QuoteUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise QuoteUnavailable(
                    "No verified current secondary perp reference."
                ) from exc
            self._reference_cache[asset] = (time.monotonic(), value)
            return value

    async def get_venue(self, asset: str, venue: str) -> VenueBookQuote:
        """Top of book for an OKX or Bitget forecast's own venue.

        Same bounds as the Binance path: 3s cache, per-venue block on
        401/403/451 (1h) or 429 (60s), fail-closed QuoteUnavailable. A
        malformed or mismatched book never degrades into another venue's
        price; the caller falls back to its WAIT verdict.
        """
        if asset not in ASSETS or venue not in VENUE_BOOK_SYMBOLS:
            raise QuoteUnavailable("Only BTC, ETH, SOL and XRP venue books supported.")
        symbol = VENUE_BOOK_SYMBOLS[venue][asset]
        async with self._venue_locks[venue]:
            clock = time.monotonic()
            if clock < self._venue_blocked_until.get(venue, 0.0):
                raise QuoteUnavailable("Venue temporarily blocked or rate limited.")
            cached = self._venue_cache.get((venue, asset))
            now = datetime.now(UTC)
            if cached is not None and clock - cached[0] <= CACHE_SECONDS:
                try:
                    cached[1].validate(now=now)
                    return cached[1]
                except QuoteUnavailable:
                    self._venue_cache.pop((venue, asset), None)
            try:
                venue_client = self._venue_clients.get(venue)
                if venue_client is None:
                    venue_client = httpx.AsyncClient(
                        base_url=VENUE_BOOK_BASE_URLS[venue],
                        timeout=httpx.Timeout(5.0),
                    )
                    self._venue_clients[venue] = venue_client
                if venue == "okx":
                    path, params = "/api/v5/market/ticker", {"instId": symbol}
                    parser = parse_okx_ticker
                else:
                    path = "/api/v2/mix/market/ticker"
                    params = {"symbol": symbol, "productType": "USDT-FUTURES"}
                    parser = parse_bitget_ticker
                resp = await venue_client.get(path, params=params)
                if resp.status_code in (401, 403, 451):
                    self._venue_blocked_until[venue] = clock + BLOCK_SECONDS
                    raise QuoteUnavailable("Venue quotes unavailable in this region/account.")
                if resp.status_code == 429:
                    self._venue_blocked_until[venue] = clock + 60
                    raise QuoteUnavailable("Venue public quote API rate limited.")
                resp.raise_for_status()
                quote = parser(resp.json(), asset=asset, received_at=datetime.now(UTC))
            except QuoteUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise QuoteUnavailable(
                    "Live executable bid/ask could not be verified."
                ) from exc
            self._venue_cache[(venue, asset)] = (time.monotonic(), quote)
            return quote


def quote_cost_preview(
    quote: BookQuote, *, quantity: float = 0.001,
    taker_fee_bps: float = 4.0, exit_slippage_bps: float = 2.0,
) -> dict:
    """Read-only immediate round-trip estimate; assumed fee, not paid receipt."""
    if any(not math.isfinite(v) or v < 0 for v in (
        quantity, taker_fee_bps, exit_slippage_bps,
    )) or quantity <= 0:
        raise ValueError("Invalid quantity or nonnegative fee assumption.")
    available = quantity <= min(quote.bid_quantity, quote.ask_quantity)
    gross = (quote.bid / quote.ask - 1) * 10000
    assumed_net = gross - 2 * taker_fee_bps - exit_slippage_bps
    return {
        "quantity": quantity,
        "top_book_sufficient": available,
        "spread_bps": quote.spread_bps,
        "immediate_round_trip_gross_bps": gross,
        "assumed_net_bps": assumed_net,
        "fee_assumed": True,
        "actual_profit_verified": False,
    }


def quote_embed(quote: BookQuote, *, quantity: float) -> discord.Embed:
    data = quote_cost_preview(quote, quantity=quantity)
    embed = discord.Embed(
        title=f"RivaL • {quote.asset} USDT • Exchange Bid/Ask",
        description=(
            "**Current read-only Binance USD-M top of book.** "
            "Not a guaranteed fill, net profit, or authenticated trade."
        ),
        color=discord.Color.blurple(),
        timestamp=quote.received_at,
    )
    embed.add_field(
        name="Buy at ask / sell at bid",
        value=f"${quote.ask:,.4f} / ${quote.bid:,.4f}", inline=False,
    )
    embed.add_field(
        name="Top-of-book available",
        value=(f"Ask {quote.ask_quantity:g} / Bid {quote.bid_quantity:g} "
               f"{quote.asset}"), inline=False,
    )
    embed.add_field(
        name=f"Hypothetical instant round trip for {quantity:g} {quote.asset}",
        value=(
            f"Spread {data['spread_bps']:.2f} bps • "
            f"estimated net {data['assumed_net_bps']:.2f} bps\n"
            f"Top-book quantity: {'sufficient' if data['top_book_sufficient'] else 'INSUFFICIENT'}"
        ), inline=False,
    )
    embed.set_footer(
        text="Fee assumption 4bps/side + 2bps slippage; no funding/fill proof "
             "• Exchange time verified ≤10s",
    )
    return embed
