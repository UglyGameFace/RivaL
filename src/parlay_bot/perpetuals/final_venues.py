"""Public Lighter and Phemex perp candles, strictly separated by venue/quote.

All access is read-only and uncredentialed. Provider-specific market metadata
must validate before OHLCV can enter RivaL's immutable market store.
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
_INTERVALS = {5: ("5m", 300), 15: ("15m", 900), 60: ("1h", 3600),
              240: ("4h", 14400)}


def _float(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MarketDataError("invalid exchange numeric field")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("invalid exchange numeric field") from exc
    if not math.isfinite(number) or number < 0 or positive and number == 0:
        raise MarketDataError("invalid zero, negative or non-finite value")
    return number


def _stamp(value: Any, *, seconds: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataError("invalid exchange timestamp")
    digits = len(str(value))
    if seconds and digits == 10:
        return value * 1000
    if not seconds and digits == 13:
        return value
    raise MarketDataError("unsupported exchange timestamp unit")


def _validate(assets: tuple[str, ...], interval: int, bars: int) -> None:
    if (not 1 <= len(assets) <= 10 or len(set(assets)) != len(assets)
        or any(_SYMBOL.fullmatch(a) is None for a in assets)
        or interval not in _INTERVALS or not 1 <= bars <= 48):
        raise ValueError("unsupported public perpetual query")


def _candle(
    *, venue: str, contract: str, quote: str, opened: int,
    earliest: int, cutoff: int, interval: int, observed: datetime,
    o: Any, high: Any, low: Any, close: Any, base_volume: Any,
) -> MarketCandle | None:
    period = interval * 60_000
    if opened % period or opened < earliest or opened > cutoff:
        raise MarketDataError("out-of-window or unaligned candle")
    if opened == cutoff:
        return None
    op, hi, lo, cl = (
        _float(v, positive=True) for v in (o, high, low, close)
    )
    if lo > min(op, cl) or hi < max(op, cl):
        raise MarketDataError("invalid exchange OHLC")
    return MarketCandle(
        venue=venue, contract=contract, quote=quote,
        interval_minutes=interval,
        opened_at=datetime.fromtimestamp(opened / 1000, UTC),
        closed_at=datetime.fromtimestamp((opened + period - 1) / 1000, UTC),
        observed_at=observed,
        open_price=op, high_price=hi, low_price=lo, close_price=cl,
        volume_base=_float(base_volume), trades=-1,
    )


class _Http:
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
            raise MarketDataError("public perpetual request failed") from exc


class LighterMarketClient(_Http):
    """Lighter public orderBooks + candles; never mistake spot rows for perps."""

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
            meta = await self._get(
                "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks", {}
            )
            entries = meta.get("order_books") if isinstance(meta, dict) else None
            if (not isinstance(meta, dict) or meta.get("code") != 200
                or not isinstance(entries, list)):
                raise MarketDataError("Lighter public perp markets unavailable")
            markets = [x for x in entries if isinstance(x, dict)
                       and x.get("symbol") == asset
                       and x.get("market_type") == "perp"
                       and x.get("status") == "active"]
            if len(markets) != 1:
                raise MarketDataError(f"Lighter active perp missing: {asset}")
            market_id = markets[0].get("market_id")
            if isinstance(market_id, bool) or not isinstance(market_id, int):
                raise MarketDataError("invalid Lighter market identity")
            result = await self._get(
                "https://mainnet.zklighter.elliot.ai/api/v1/candles",
                {"market_id": market_id, "resolution": _INTERVALS[interval_minutes][0],
                 "start_timestamp": earliest // 1000,
                 "end_timestamp": cutoff // 1000,
                 "count_back": bars},
            )
            rows = result.get("c") if isinstance(result, dict) else None
            if (not isinstance(result, dict) or result.get("code") != 200
                or result.get("r") != _INTERVALS[interval_minutes][0]
                or not isinstance(rows, list) or len(rows) > bars + 1):
                raise MarketDataError("invalid Lighter candle response")
            previous = -1
            added = 0
            for row in sorted(rows, key=lambda x: x.get("t", -1) if isinstance(x, dict) else -1):
                if not isinstance(row, dict):
                    raise MarketDataError("invalid Lighter row")
                opened = _stamp(row.get("t"))
                if opened <= previous:
                    raise MarketDataError("duplicate Lighter candle")
                previous = opened
                candle = _candle(
                    venue="lighter", contract=asset, quote="USD",
                    opened=opened, earliest=earliest, cutoff=cutoff,
                    interval=interval_minutes, observed=started,
                    o=row.get("o"), high=row.get("h"), low=row.get("l"),
                    close=row.get("c"), base_volume=row.get("v"),
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError(f"no completed Lighter candles: {asset}")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            replace(c, observed_at=observed) for c in candles
        ), interval_minutes, observed)


class PhemexMarketClient(_Http):
    """Phemex V2 linear USDT perpetual products with unscaled base-volume klines."""

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
            symbol = f"{asset}USDT"
            meta = await self._get("https://api.phemex.com/public/products", {})
            data = meta.get("data") if isinstance(meta, dict) else None
            products = data.get("perpProductsV2") if isinstance(data, dict) else None
            if (not isinstance(meta, dict) or meta.get("code") != 0
                or not isinstance(products, list)):
                raise MarketDataError("Phemex public linear products unavailable")
            valid = [
                item for item in products if isinstance(item, dict)
                and item.get("symbol") == symbol
                and item.get("type") == "PerpetualV2"
                and item.get("status") == "Listed"
                and item.get("baseCurrency") == asset
                and item.get("quoteCurrency") == "USDT"
                and item.get("settleCurrency") == "USDT"
            ]
            if len(valid) != 1:
                raise MarketDataError(f"Phemex USDT perpetual unavailable: {symbol}")
            response = await self._get(
                "https://api.phemex.com/exchange/public/md/v2/kline/last",
                {"symbol": symbol, "resolution": _INTERVALS[interval_minutes][1],
                 "limit": 50},
            )
            body = response.get("data") if isinstance(response, dict) else None
            rows = body.get("rows") if isinstance(body, dict) else None
            if (not isinstance(response, dict) or response.get("code") != 0
                or not isinstance(rows, list) or len(rows) > 50):
                raise MarketDataError("invalid Phemex futures kline response")
            previous = -1
            added = 0
            for row in sorted(rows, key=lambda x: x[0] if isinstance(x, list) and x else -1):
                if not isinstance(row, list) or len(row) < 9:
                    raise MarketDataError("invalid Phemex candle")
                opened = _stamp(row[0], seconds=True)
                if opened <= previous:
                    raise MarketDataError("duplicate Phemex candle timestamp")
                previous = opened
                if row[1] != _INTERVALS[interval_minutes][1]:
                    raise MarketDataError("Phemex candle interval mismatch")
                if opened < earliest:
                    continue
                candle = _candle(
                    venue="phemex", contract=symbol, quote="USDT",
                    opened=opened, earliest=earliest, cutoff=cutoff,
                    interval=interval_minutes, observed=started,
                    o=row[3], high=row[4], low=row[5], close=row[6],
                    base_volume=row[7],
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError(f"no completed Phemex candles: {symbol}")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            replace(c, observed_at=observed) for c in candles
        ), interval_minutes, observed)
