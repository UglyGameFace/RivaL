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
from typing import Any

import discord
import httpx

ASSETS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
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


class PublicBestQuoteClient:
    """Bounded shared REST snapshot; no API keys, wallet or order endpoint."""

    def __init__(
        self, *, client: httpx.AsyncClient | None = None,
        base_url: str = "https://fapi.binance.com",
    ) -> None:
        self._owns = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(5.0),
        )
        self._cache: dict[str, tuple[float, BookQuote]] = {}
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    async def aclose(self) -> None:
        if self._owns:
            await self.client.aclose()

    async def get(self, asset: str) -> BookQuote:
        if asset not in ASSETS:
            raise QuoteUnavailable("Only BTC, ETH and SOL USDT futures supported.")
        async with self._lock:
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
