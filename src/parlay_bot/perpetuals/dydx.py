"""Read-only, bounded dYdX Chain v4 Indexer perpetual candle collector.

This source provides USD-quoted candles; it must never be relabeled USDC
or assigned Hyperliquid-specific mark/funding/open-interest context.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.hyperliquid import (
    MarketBatch,
    MarketCandle,
    MarketDataError,
)

INDEXER_URL = "https://indexer.dydx.trade/v4"
RESOLUTIONS = {5: "5MINS", 15: "15MINS", 60: "1HOUR", 240: "4HOURS"}
MAX_BARS = 99
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MarketDataError("invalid dYdX numeric field")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("invalid dYdX numeric field") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise MarketDataError("invalid dYdX numeric field")
    return number


def _trades(value: Any) -> int:
    if isinstance(value, bool) or (
        not isinstance(value, int)
        and (not isinstance(value, str) or not value.isascii() or not value.isdecimal())
    ):
        raise MarketDataError("invalid dYdX trade count")
    number = int(value)
    if number < 0:
        raise MarketDataError("invalid dYdX trade count")
    return number


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MarketDataError("invalid dYdX candle timestamp")
    try:
        stamp = datetime.fromisoformat(value)
        return utc(stamp)
    except ValueError as exc:
        raise MarketDataError("invalid dYdX candle timestamp") from exc


class DydxMarketClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _get(self, path: str, *, params: dict[str, str | int] | None = None) -> Any:
        try:
            response = await self._client.get(
                f"{INDEXER_URL}/{path}", params=params, follow_redirects=False
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # A regional block or rate limit must not be routed around.
            raise MarketDataError("dYdX public market request failed") from exc

    async def collect(
        self,
        *,
        assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        interval_minutes: int = 5,
        bars: int = 48,
        as_of: datetime | None = None,
    ) -> MarketBatch:
        if interval_minutes not in RESOLUTIONS or not 1 <= bars <= MAX_BARS:
            raise ValueError("invalid dYdX interval/history bounds")
        if (
            not assets or len(assets) > 10
            or len(assets) != len(set(assets))
            or any(_SYMBOL.fullmatch(asset) is None for asset in assets)
        ):
            raise ValueError("use 1-10 unique uppercase dYdX asset symbols")
        started_at = utc(as_of if as_of is not None else datetime.now(UTC))
        interval = timedelta(minutes=interval_minutes)
        cutoff = datetime.fromtimestamp(
            int(started_at.timestamp()) // (interval_minutes * 60)
            * interval_minutes * 60, UTC
        )
        earliest = cutoff - bars * interval
        markets_response = await self._get("perpetualMarkets")
        if (
            not isinstance(markets_response, dict)
            or not isinstance(markets_response.get("markets"), dict)
        ):
            raise MarketDataError("invalid dYdX perpetual markets response")
        markets = markets_response["markets"]
        candles: list[MarketCandle] = []
        resolution = RESOLUTIONS[interval_minutes]
        for asset in assets:
            ticker = f"{asset}-USD"
            market = markets.get(ticker)
            if (
                not isinstance(market, dict)
                or market.get("ticker") != ticker
                or market.get("status") != "ACTIVE"
            ):
                raise MarketDataError(f"active dYdX perpetual unavailable: {ticker}")
            result = await self._get(
                f"candles/perpetualMarkets/{ticker}",
                params={"resolution": resolution, "limit": bars + 1},
            )
            if not isinstance(result, dict) or not isinstance(result.get("candles"), list):
                raise MarketDataError("invalid dYdX candle response")
            rows = result["candles"]
            if len(rows) > bars + 1:
                raise MarketDataError("unbounded dYdX candle response")
            previous: datetime | None = None
            parsed: list[MarketCandle] = []
            for row in sorted(rows, key=lambda item: item.get("startedAt", "")
                              if isinstance(item, dict) else ""):
                if not isinstance(row, dict):
                    raise MarketDataError("invalid dYdX candle")
                returned_ticker = row.get("ticker", row.get("market"))
                if returned_ticker != ticker or row.get("resolution") != resolution:
                    raise MarketDataError("mismatched dYdX candle contract or interval")
                opened = _timestamp(row.get("startedAt"))
                if previous is not None and opened <= previous:
                    raise MarketDataError("duplicate/out-of-order dYdX candle")
                previous = opened
                if int(opened.timestamp()) % (interval_minutes * 60):
                    raise MarketDataError("unaligned dYdX candle")
                if opened >= cutoff:
                    if opened < cutoff + interval:
                        continue  # Current candle has not finished.
                    raise MarketDataError("future-dated dYdX candle")
                if opened < earliest:
                    continue  # Older history is allowed but not silently imported.
                opened_price, high, low, close = (
                    _number(row.get(field), positive=True)
                    for field in ("open", "high", "low", "close")
                )
                if low > min(opened_price, close) or high < max(opened_price, close):
                    raise MarketDataError("inconsistent dYdX OHLC")
                volume = _number(row.get("baseTokenVolume"))
                if volume < 0:
                    raise MarketDataError("negative dYdX base volume")
                parsed.append(MarketCandle(
                    venue="dydx",
                    contract=ticker,
                    quote="USD",
                    interval_minutes=interval_minutes,
                    opened_at=opened,
                    closed_at=opened + interval - timedelta(milliseconds=1),
                    observed_at=started_at,
                    open_price=opened_price,
                    high_price=high,
                    low_price=low,
                    close_price=close,
                    volume_base=volume,
                    trades=_trades(row.get("trades")),
                ))
            if not parsed:
                raise MarketDataError(f"no complete dYdX candle returned for {ticker}")
            candles.extend(parsed)
        available_at = started_at if as_of is not None else datetime.now(UTC)
        return MarketBatch(
            contexts=(),  # Not available with compatible mark/funding semantics.
            candles=tuple(replace(c, observed_at=available_at) for c in candles),
            interval_minutes=interval_minutes,
            observed_at=available_at,
        )
