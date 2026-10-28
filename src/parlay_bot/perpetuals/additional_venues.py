"""No-key Deribit linear USDC perpetual and Binance USD-M public candle feeds.

Read-only market data. Explicit instrument validation and bounded, closed
candles prevent accidentally collecting spot, expiry futures or inverse volume.
Binance may be region blocked; the caller's source cooldown applies.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_RESOLUTIONS = {5: ("5", "5m"), 15: ("15", "15m"),
                60: ("60", "1h"), 240: ("240", "4h")}


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        raise MarketDataError("invalid derivatives numeric field")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("invalid derivatives numeric field") from exc
    if not math.isfinite(number) or number < 0 or positive and number == 0:
        raise MarketDataError("negative, non-finite or zero price/volume")
    return number


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1e12:
        raise MarketDataError("invalid millisecond timestamp")
    return value


def _validate(assets: tuple[str, ...], interval: int, bars: int) -> None:
    if (not 1 <= len(assets) <= 10 or len(assets) != len(set(assets))
        or any(_SYMBOL.fullmatch(s) is None for s in assets)
        or interval not in _RESOLUTIONS or not 1 <= bars <= 48):
        raise ValueError("unsupported public derivatives collection request")


def _make(
    venue: str, contract: str, quote: str, ts: int, earliest: int,
    cutoff: int, interval: int, observed: datetime,
    o: Any, high: Any, low: Any, close: Any, volume_base: Any,
    trades: int = -1,
) -> MarketCandle | None:
    duration = interval * 60_000
    if ts < earliest or ts > cutoff or ts % duration:
        raise MarketDataError("misaligned or unbounded derivatives candle")
    if ts == cutoff:
        return None
    op, hi, lo, cl = (_number(v, positive=True) for v in (o, high, low, close))
    if lo > min(op, cl) or hi < max(op, cl):
        raise MarketDataError("inconsistent derivatives OHLC")
    return MarketCandle(
        venue=venue, contract=contract, quote=quote,
        interval_minutes=interval,
        opened_at=datetime.fromtimestamp(ts / 1000, UTC),
        closed_at=datetime.fromtimestamp((ts + duration - 1) / 1000, UTC),
        observed_at=observed,
        open_price=op, high_price=hi, low_price=lo, close_price=cl,
        volume_base=_number(volume_base), trades=trades,
    )


class _PublicHttp:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(url, params=params, follow_redirects=False)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("public derivatives request failed") from exc


class DeribitMarketClient(_PublicHttp):
    """USD quoted USDC linear perpetuals; chart volume is base-coin volume."""

    async def collect(
        self, *, assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        interval_minutes: int = 5, bars: int = 48,
    ) -> MarketBatch:
        _validate(assets, interval_minutes, bars)
        started = datetime.now(UTC)
        period = interval_minutes * 60_000
        cutoff = int(started.timestamp() * 1000) // period * period
        earliest = cutoff - bars * period
        candles: list[MarketCandle] = []
        for asset in assets:
            contract = f"{asset}_USDC-PERPETUAL"
            metadata = await self._get(
                "https://www.deribit.com/api/v2/public/get_instrument",
                {"instrument_name": contract},
            )
            instrument = (
                metadata.get("result") if isinstance(metadata, dict) else None
            )
            if (
                not isinstance(instrument, dict)
                or instrument.get("instrument_name") != contract
                or instrument.get("kind") != "future"
                or instrument.get("settlement_period") != "perpetual"
                or instrument.get("instrument_type") != "linear"
                or instrument.get("counter_currency") != "USDC"
                or instrument.get("settlement_currency") != "USDC"
                or instrument.get("is_active") is not True
                or instrument.get("state", "open") != "open"
            ):
                raise MarketDataError("Deribit active linear USDC perp unavailable")
            data = await self._get(
                "https://www.deribit.com/api/v2/public/get_tradingview_chart_data",
                {"instrument_name": contract, "start_timestamp": earliest,
                 "end_timestamp": cutoff - 1,
                 "resolution": _RESOLUTIONS[interval_minutes][0]},
            )
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise MarketDataError("Deribit chart unavailable")
            names = ("ticks", "open", "high", "low", "close", "volume")
            series = [result.get(name) for name in names]
            if not all(isinstance(v, list) for v in series):
                raise MarketDataError("Deribit chart arrays missing")
            ticks, opens, highs, lows, closes, volumes = series
            if not 1 <= len(ticks) <= bars + 1 or any(
                len(values) != len(ticks) for values in series
            ):
                raise MarketDataError("Deribit chart arrays misaligned")
            previous = -1
            added = 0
            for i, raw in enumerate(ticks):
                ts = _timestamp(raw)
                if ts <= previous:
                    raise MarketDataError("duplicate/out-of-order Deribit bar")
                previous = ts
                candle = _make(
                    "deribit", contract, "USDC", ts, earliest, cutoff,
                    interval_minutes, started, opens[i], highs[i], lows[i],
                    closes[i], volumes[i],
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError(f"no completed Deribit candles for {contract}")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            replace(x, observed_at=observed) for x in candles
        ), interval_minutes, observed)


class BinanceFuturesMarketClient(_PublicHttp):
    """Binance USD-M *perpetual* market data. Do not bypass HTTP 403/451."""

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
            info = await self._get(
                "https://fapi.binance.com/fapi/v1/exchangeInfo", {}
            )
            symbols = info.get("symbols") if isinstance(info, dict) else None
            if not isinstance(symbols, list):
                raise MarketDataError("Binance futures symbols unavailable")
            found = [s for s in symbols if isinstance(s, dict)
                     and s.get("symbol") == symbol]
            if len(found) != 1 or any(
                found[0].get(key) != value
                for key, value in (
                    ("contractType", "PERPETUAL"), ("status", "TRADING"),
                    ("baseAsset", asset), ("quoteAsset", "USDT"),
                    ("marginAsset", "USDT"),
                )
            ):
                raise MarketDataError("Binance USDT perpetual symbol unavailable")
            rows = await self._get(
                "https://fapi.binance.com/fapi/v1/klines",
                {"symbol": symbol, "interval": _RESOLUTIONS[interval_minutes][1],
                 "limit": bars + 1},
            )
            if not isinstance(rows, list) or len(rows) > bars + 1:
                raise MarketDataError("invalid Binance futures klines")
            previous = -1
            added = 0
            for row in rows:
                if not isinstance(row, list) or len(row) < 9:
                    raise MarketDataError("invalid Binance futures kline row")
                ts = _timestamp(row[0])
                if ts <= previous:
                    raise MarketDataError("out-of-order/duplicate Binance candle")
                previous = ts
                if ts < earliest:
                    continue
                trades = row[8]
                if not isinstance(trades, int) or isinstance(trades, bool) or trades < 0:
                    raise MarketDataError("invalid Binance trade count")
                candle = _make(
                    "binance", symbol, "USDT", ts, earliest, cutoff,
                    interval_minutes, started,
                    row[1], row[2], row[3], row[4], row[5],
                    trades=trades,
                )
                if candle is not None:
                    if row[6] != ts + duration - 1:
                        raise MarketDataError("Binance bar closing timestamp mismatch")
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError(f"no completed Binance candles for {symbol}")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            replace(x, observed_at=observed) for x in candles
        ), interval_minutes, observed)
