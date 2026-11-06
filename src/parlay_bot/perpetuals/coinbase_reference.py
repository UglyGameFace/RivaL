"""Live coin price and 1-minute candles from Coinbase's public market data.

Used only as the price input to the Up/Down formula. Fails closed: a stale or
malformed answer returns None, never an estimate from another source.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import UTC, datetime, timedelta

import httpx

BASE = "https://api.exchange.coinbase.com"
TICKER_MAX_AGE = timedelta(seconds=15)
CANDLE_REFRESH = 50.0


class CoinbaseReference:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._own = client is None
        self._candles: dict[str, tuple[float, list[tuple[int, float, float]]]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=6.0, headers={"User-Agent": "RivaL/1.0"})
        return self._client

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def price(self, asset: str, *, now: datetime | None = None) -> float | None:
        clock = now or datetime.now(UTC)
        try:
            response = await self._http().get(f"{BASE}/products/{asset}-USD/ticker")
            response.raise_for_status()
            data = response.json()
            value = float(data["price"])
            # Trim sub-microsecond digits so every Python version parses it.
            raw = re.sub(r"(\.\d{6})\d+", r"\1", str(data["time"]))
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # noqa: FURB162
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        if not (math.isfinite(value) and value > 0) or abs(clock - stamp) > TICKER_MAX_AGE:
            return None
        return value

    async def candles(self, asset: str) -> list[tuple[int, float, float]] | None:
        """[(start_unix, open, close)] oldest first, refreshed about once a minute."""
        saved = self._candles.get(asset)
        if saved is not None and time.monotonic() - saved[0] < CANDLE_REFRESH:
            return saved[1]
        try:
            response = await self._http().get(
                f"{BASE}/products/{asset}-USD/candles", params={"granularity": 60})
            response.raise_for_status()
            rows = response.json()
            parsed = sorted(
                (int(r[0]), float(r[3]), float(r[4])) for r in rows
                if isinstance(r, list) and len(r) >= 5
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            return saved[1] if saved else None
        if len(parsed) < 20:
            return saved[1] if saved else None
        self._candles[asset] = (time.monotonic(), parsed)
        return parsed

    @staticmethod
    def open_at(candles: list[tuple[int, float, float]], opens: datetime) -> float | None:
        """The coin's price at the contract's open: that minute's opening trade."""
        target = int(opens.timestamp())
        for start, open_price, _ in candles:
            if start == target and math.isfinite(open_price) and open_price > 0:
                return open_price
        return None


async def gather_prices(reference: CoinbaseReference, assets) -> dict[str, float | None]:
    values = await asyncio.gather(*(reference.price(a) for a in assets))
    return dict(zip(assets, values, strict=True))
