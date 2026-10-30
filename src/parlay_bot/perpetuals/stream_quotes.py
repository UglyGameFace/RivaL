"""Bounded, reconnecting public perpetual order-book streams.

Official Hyperliquid l2Book and Binance USD-M market/bookTicker wire formats.
Snapshots remain in RAM, never upload raw ticks to PostgreSQL or Google Drive.
REST and existing five-minute candle collection remain independent fallbacks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from typing import Any

import websockets

from parlay_bot.perpetuals.live_quotes import (
    ASSETS,
    BookQuote,
    PerpReferenceBook,
    QuoteUnavailable,
    parse_hyperliquid_reference,
)

_LOG = logging.getLogger(__name__)
HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"
BINANCE_WS = "wss://fstream.binance.com/market/stream?streams=" + "/".join(
    f"{symbol.lower()}@bookTicker" for symbol in ASSETS.values()
)
VENUES = ("hyperliquid", "binance")
RECONNECT_CAP_SECONDS = 60.0


def parse_stream_message(
    venue: str, message: str | bytes, *, received_at: datetime,
) -> PerpReferenceBook | BookQuote | None:
    """Only admit timestamped, source-identified, viable best-book updates."""
    if isinstance(message, bytes):
        raise QuoteUnavailable("Binary market stream frame not supported.")
    try:
        wrapper: Any = json.loads(message)
    except (ValueError, TypeError) as exc:
        raise QuoteUnavailable("Invalid market stream JSON.") from exc
    if not isinstance(wrapper, dict):
        raise QuoteUnavailable("Market stream must be an object.")
    if venue == "hyperliquid":
        if wrapper.get("channel") != "l2Book":
            return None
        payload = wrapper.get("data")
        if not isinstance(payload, dict):
            raise QuoteUnavailable("Malformed Hyperliquid book event.")
        asset = payload.get("coin")
        if asset not in ASSETS:
            return None
        return parse_hyperliquid_reference(
            payload, asset=asset, received_at=received_at,
        )
    if venue != "binance":
        raise QuoteUnavailable("Unsupported market stream.")
    raw = wrapper.get("data", wrapper)
    if not isinstance(raw, dict) or raw.get("e") != "bookTicker":
        return None
    if raw.get("st") not in (None, 1):
        return None  # COIN-M rows cannot masquerade as USD-M perp books.
    symbol = raw.get("s")
    if symbol not in ASSETS.values():
        return None
    if type(raw.get("E")) is not int or raw["E"] <= 0:
        raise QuoteUnavailable("Missing Binance book event time.")
    try:
        quote = BookQuote(
            asset=symbol.removesuffix("USDT"),
            venue="binance_usdm",
            symbol=symbol,
            bid=float(raw["b"]), bid_quantity=float(raw["B"]),
            ask=float(raw["a"]), ask_quantity=float(raw["A"]),
            exchange_at=datetime.fromtimestamp(raw["E"] / 1000, tz=UTC),
            received_at=received_at,
        )
        quote.validate(now=received_at)
        return quote
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise QuoteUnavailable("Corrupt Binance book event.") from exc


class StreamingBookHub:
    """One bounded socket per provider, no per-user subscription fanout."""

    def __init__(self, *, assets: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP")) -> None:
        if not assets or len(set(assets)) != len(assets) or any(
            asset not in ASSETS for asset in assets
        ):
            raise ValueError("Invalid perpetual stream assets")
        self.assets = assets
        self._quotes: dict[tuple[str, str], BookQuote | PerpReferenceBook] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._connected: dict[str, bool] = dict.fromkeys(VENUES, False)
        self._reconnects: dict[str, int] = dict.fromkeys(VENUES, 0)
        self._accepted: dict[str, int] = dict.fromkeys(VENUES, 0)

    def snapshot(self, venue: str, asset: str, *, now: datetime | None = None
                 ) -> BookQuote | PerpReferenceBook | None:
        if venue not in VENUES or asset not in self.assets:
            return None
        clock = now or datetime.now(UTC)
        item = self._quotes.get((venue, asset))
        if item is None:
            return None
        try:
            item.validate(now=clock)
        except QuoteUnavailable:
            return None
        return item

    def health(self, *, now: datetime | None = None) -> dict:
        clock = now or datetime.now(UTC)
        return {
            venue: {
                "connected": self._connected[venue],
                "updates": self._accepted[venue],
                "reconnects": self._reconnects[venue],
                "fresh_assets": tuple(
                    a for a in self.assets
                    if self.snapshot(venue, a, now=clock) is not None
                ),
            }
            for venue in VENUES
        }

    def accept(
        self, venue: str, message: str | bytes, *,
        received_at: datetime | None = None,
    ) -> bool:
        now = received_at or datetime.now(UTC)
        try:
            quote = parse_stream_message(venue, message, received_at=now)
        except QuoteUnavailable:
            return False
        if quote is None or quote.asset not in self.assets:
            return False
        key = (venue, quote.asset)
        previous = self._quotes.get(key)
        if previous is not None and quote.exchange_at <= previous.exchange_at:
            return False  # No replayed/out-of-order or stale snapshot.
        self._quotes[key] = quote
        self._accepted[venue] += 1
        return True

    async def _run(self, venue: str) -> None:
        url = HYPERLIQUID_WS if venue == "hyperliquid" else (
            "wss://fstream.binance.com/market/stream?streams="
            + "/".join(
                f"{ASSETS[asset].lower()}@bookTicker" for asset in self.assets
            )
        )
        attempts = 0
        while True:
            backoff_override = 0.0
            try:
                async with websockets.connect(
                    url, open_timeout=10, close_timeout=5,
                    ping_interval=20, ping_timeout=20,
                    max_size=1_048_576, max_queue=32,
                ) as socket:
                    self._connected[venue] = True
                    attempts = 0
                    if venue == "hyperliquid":
                        for asset in self.assets:
                            await socket.send(json.dumps({
                                "method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": asset},
                            }))
                    async for message in socket:
                        self.accept(venue, message)
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.InvalidStatus as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (401, 403, 418, 451):
                    backoff_override = 3600.0
                    _LOG.warning("RivaL %s stream region/access blocked (HTTP %s)",
                                 venue, status)
                elif status == 429:
                    backoff_override = 300.0
                    _LOG.warning("RivaL %s stream rate limited", venue)
                else:
                    _LOG.warning("RivaL %s stream handshake unavailable", venue)
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException,
                    ValueError):
                _LOG.warning("RivaL %s stream disconnected; retrying safely", venue)
            finally:
                self._connected[venue] = False
            attempts += 1
            self._reconnects[venue] += 1
            await asyncio.sleep(
                max(backoff_override, min(RECONNECT_CAP_SECONDS, 2 ** min(attempts, 5)))
                + random.uniform(0, 1)
            )

    def start(self) -> None:
        if self._tasks:
            return
        for venue in VENUES:
            self._tasks.append(asyncio.create_task(
                self._run(venue), name=f"rival-{venue}-book-stream",
            ))

    async def aclose(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connected = dict.fromkeys(VENUES, False)
