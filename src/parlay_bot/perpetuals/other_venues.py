"""Two independently verified *documented* public USDT perpetual candle APIs.

Bitget V3 reports base volume; Gate V4 reports contract-count volume, which
must be multiplied by the public direct-contract quanto_multiplier.
No account, key, order endpoint, fake trade counts or funding is used.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from parlay_bot.perpetuals.hyperliquid import (
    MarketBatch,
    MarketCandle,
    MarketDataError,
)

_ASSET = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_INTERVAL = {5: ("5m", "5m"), 15: ("15m", "15m"), 60: ("1H", "1h"), 240: ("4H", "4h")}


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MarketDataError("invalid source numeric value")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("invalid source numeric value") from exc
    if not math.isfinite(number) or (positive and number <= 0) or (not positive and number < 0):
        raise MarketDataError("invalid source numeric value")
    return number


def _validate(assets: tuple[str, ...], interval: int, bars: int) -> None:
    if (
        not 1 <= len(assets) <= 10 or len(set(assets)) != len(assets)
        or any(_ASSET.fullmatch(x) is None for x in assets)
        or interval not in _INTERVAL or not 1 <= bars <= 48
    ):
        raise ValueError("invalid public futures request")


def _candle(
    venue: str, contract: str, opened_ms: int, start_ms: int, cutoff_ms: int,
    interval_minutes: int, observed: datetime, o: Any, h: Any,
    low: Any, c: Any, base_volume: Any,
) -> MarketCandle | None:
    duration = interval_minutes * 60_000
    if opened_ms % duration or opened_ms < start_ms or opened_ms > cutoff_ms:
        raise MarketDataError("outside requested candle boundaries")
    if opened_ms == cutoff_ms:
        return None  # Still-forming bar.
    op, high, lo, close = (
        _number(v, positive=True) for v in (o, h, low, c)
    )
    if lo > min(op, close) or high < max(op, close):
        raise MarketDataError("invalid source OHLC")
    return MarketCandle(
        venue=venue, contract=contract, quote="USDT",
        interval_minutes=interval_minutes,
        opened_at=datetime.fromtimestamp(opened_ms / 1000, UTC),
        closed_at=datetime.fromtimestamp((opened_ms + duration - 1) / 1000, UTC),
        observed_at=observed, open_price=op, high_price=high,
        low_price=lo, close_price=close,
        volume_base=_number(base_volume),
        trades=-1,  # -1 = unknown; do NOT interpret as zero actual trades.
    )


class _HttpSource:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            result = await self._client.get(url, params=params, follow_redirects=False)
            result.raise_for_status()
            return result.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("public futures request failed") from exc


class BitgetMarketClient(_HttpSource):
    """Official Bitget V3 USDT-FUTURES public instruments + candle endpoints."""

    async def collect(
        self, *, assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        interval_minutes: int = 5, bars: int = 48,
    ) -> MarketBatch:
        _validate(assets, interval_minutes, bars)
        started = datetime.now(UTC)
        duration = interval_minutes * 60_000
        cutoff = int(started.timestamp() * 1000) // duration * duration
        earliest = cutoff - bars * duration
        candles: list[MarketCandle] = []
        for asset in assets:
            symbol = f"{asset}USDT"
            instrument = await self._get(
                "https://api.bitget.com/api/v3/market/instruments",
                {"category": "USDT-FUTURES", "symbol": symbol},
            )
            if (
                not isinstance(instrument, dict) or instrument.get("code") != "00000"
                or not isinstance(instrument.get("data"), list)
                or not any(
                    isinstance(x, dict) and x.get("symbol") == symbol
                    and x.get("category") == "USDT-FUTURES"
                    and x.get("type") == "perpetual" and x.get("status") == "online"
                    and x.get("baseCoin") == asset and x.get("quoteCoin") == "USDT"
                    for x in instrument["data"]
                )
            ):
                raise MarketDataError("Bitget active perpetual instrument unavailable")
            result = await self._get(
                "https://api.bitget.com/api/v3/market/candles",
                {"category": "USDT-FUTURES", "symbol": symbol,
                 "interval": _INTERVAL[interval_minutes][0],
                 "startTime": str(earliest),
                 "endTime": str(cutoff - 1), "limit": str(bars + 1)},
            )
            if (
                not isinstance(result, dict) or result.get("code") != "00000"
                or not isinstance(result.get("data"), list)
                or len(result["data"]) > bars + 1
            ):
                raise MarketDataError("invalid Bitget candle response")
            previous = -1
            added = 0
            for row in sorted(result["data"], key=lambda x: x[0] if isinstance(x, list) and x else ""):
                if not isinstance(row, list) or len(row) < 7:
                    raise MarketDataError("invalid Bitget candle row")
                try:
                    stamp = int(row[0])
                except (ValueError, TypeError, OverflowError) as exc:
                    raise MarketDataError("invalid Bitget timestamp") from exc
                if stamp <= previous:
                    raise MarketDataError("duplicate Bitget candle")
                previous = stamp
                candle = _candle(
                    "bitget", symbol, stamp, earliest, cutoff,
                    interval_minutes, started, *row[1:6],
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if added == 0:
                raise MarketDataError("Bitget returned no completed candles")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(replace(c, observed_at=observed) for c in candles), interval_minutes, observed)


class GateMarketClient(_HttpSource):
    """Official Gate Futures V4 USDT direct-contract OHLCV API."""

    async def collect(
        self, *, assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        interval_minutes: int = 5, bars: int = 48,
    ) -> MarketBatch:
        _validate(assets, interval_minutes, bars)
        started = datetime.now(UTC)
        duration = interval_minutes * 60_000
        cutoff = int(started.timestamp() * 1000) // duration * duration
        earliest = cutoff - bars * duration
        candles: list[MarketCandle] = []
        for asset in assets:
            symbol = f"{asset}_USDT"
            contract = await self._get(
                f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}", {},
            )
            if (
                not isinstance(contract, dict) or contract.get("name") != symbol
                or contract.get("type") != "direct"
                or contract.get("status") != "trading"
                or contract.get("in_delisting", False)
            ):
                raise MarketDataError("Gate active direct USDT perpetual unavailable")
            multiplier = _number(contract.get("quanto_multiplier"), positive=True)
            result = await self._get(
                "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                {"contract": symbol, "interval": _INTERVAL[interval_minutes][1],
                 "limit": bars + 1},
            )
            if not isinstance(result, list) or len(result) > bars + 1:
                raise MarketDataError("invalid Gate candle response")
            previous = -1
            added = 0
            for row in sorted(result, key=lambda x: x.get("t", -1) if isinstance(x, dict) else -1):
                if not isinstance(row, dict) or isinstance(row.get("t"), bool):
                    raise MarketDataError("invalid Gate candle row")
                try:
                    timestamp = int(row["t"]) * 1000
                except (TypeError, KeyError, ValueError, OverflowError) as exc:
                    raise MarketDataError("invalid Gate candle timestamp") from exc
                if timestamp <= previous:
                    raise MarketDataError("duplicate Gate candle")
                previous = timestamp
                if timestamp < earliest:
                    continue  # Gate may return a wider recent window.
                candle = _candle(
                    "gate", symbol, timestamp, earliest, cutoff,
                    interval_minutes, started, row.get("o"), row.get("h"),
                    row.get("l"), row.get("c"),
                    _number(row.get("v")) * multiplier,
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError("Gate returned no completed candles")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            MarketCandle(**{**c.__dict__, "observed_at": observed}) for c in candles
        ), interval_minutes, observed)
