"""Public OKX USDT swaps and KuCoin USDTM perpetuals, with base-volume provenance.

Both are optional read-only sources. A blocked or schema-incompatible venue
fails closed while the existing four source workers continue collecting.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError

_ASSET = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_INTERVALS = {5: ("5m", "5min"), 15: ("15m", "15min"),
              60: ("1H", "1hour"), 240: ("4H", "4hour")}


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MarketDataError("invalid numeric field")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("invalid numeric field") from exc
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise MarketDataError("non-finite or negative value")
    return number


def _timestamp(value: Any, *, kucoin: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MarketDataError("invalid candle timestamp")
    s = str(value)
    if not s.isascii() or not s.isdecimal():
        raise MarketDataError("invalid candle timestamp")
    raw = int(s)
    if kucoin:
        if len(s) == 10:
            return raw * 1000
        if len(s) == 13:
            return raw
    elif len(s) == 13:
        return raw
    raise MarketDataError("unexpected timestamp unit")


def _validate(assets: tuple[str, ...], interval: int, bars: int) -> None:
    if (not 1 <= len(assets) <= 10 or len(set(assets)) != len(assets)
        or any(_ASSET.fullmatch(a) is None for a in assets)
        or interval not in _INTERVALS or not 1 <= bars <= 48):
        raise ValueError("invalid public futures market request")


def _make_candle(
    *, venue: str, contract: str, opened: int, earliest: int,
    cutoff: int, interval: int, now: datetime, prices: tuple[Any, ...],
    base_volume: float,
) -> MarketCandle | None:
    duration = interval * 60_000
    if opened % duration or opened < earliest or opened > cutoff:
        raise MarketDataError("candle timestamp outside aligned request range")
    if opened == cutoff:
        return None  # Current bar is unclosed.
    o, h, low, c = (_number(v, positive=True) for v in prices)
    if low > min(o, c) or h < max(o, c):
        raise MarketDataError("inconsistent futures OHLC")
    return MarketCandle(
        venue=venue, contract=contract, quote="USDT",
        interval_minutes=interval,
        opened_at=datetime.fromtimestamp(opened / 1000, UTC),
        closed_at=datetime.fromtimestamp((opened + duration - 1) / 1000, UTC),
        observed_at=now, open_price=o, high_price=h, low_price=low,
        close_price=c, volume_base=base_volume,
        trades=-1,  # Unknown; never fabricate a zero trade-count.
    )


class _HttpFeed:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(url, params=params, follow_redirects=False)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("public futures request failed") from exc


class OkxMarketClient(_HttpFeed):
    """Official OKX V5 live linear SWAP contracts with public OHLCV."""

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
            contract = f"{asset}-USDT-SWAP"
            meta = await self._get("https://www.okx.com/api/v5/public/instruments",
                                   {"instType": "SWAP", "instId": contract})
            if (not isinstance(meta, dict) or meta.get("code") != "0"
                or not isinstance(meta.get("data"), list)
                or len(meta["data"]) != 1):
                raise MarketDataError("OKX instrument metadata unavailable")
            inst = meta["data"][0]
            if (not isinstance(inst, dict) or inst.get("instId") != contract
                or inst.get("instType") != "SWAP" or inst.get("state") != "live"
                or inst.get("ctType") != "linear"
                or inst.get("settleCcy") != "USDT"
                or inst.get("ctValCcy") != asset):
                raise MarketDataError("OKX linear swap contract mismatch")
            base_per_contract = (
                _number(inst.get("ctVal"), positive=True)
                * _number(inst.get("ctMult"), positive=True)
            )
            data = await self._get(
                "https://www.okx.com/api/v5/market/candles",
                {"instId": contract, "bar": _INTERVALS[interval_minutes][0],
                 "limit": str(bars + 1)},
            )
            if (not isinstance(data, dict) or data.get("code") != "0"
                or not isinstance(data.get("data"), list)
                or len(data["data"]) > bars + 1):
                raise MarketDataError("invalid OKX candles")
            previous = -1
            added = 0
            for row in sorted(data["data"], key=lambda x: x[0] if isinstance(x, list) and x else ""):
                if not isinstance(row, list) or len(row) < 9:
                    raise MarketDataError("invalid OKX candle row")
                ts = _timestamp(row[0])
                if ts <= previous:
                    raise MarketDataError("duplicate OKX candle timestamp")
                previous = ts
                if ts < earliest:
                    continue  # Bounded lookback, never import extra older data.
                if row[8] != "1":
                    if ts == cutoff:
                        continue
                    raise MarketDataError("unconfirmed historical OKX candle")
                candle = _make_candle(
                    venue="okx", contract=contract, opened=ts, earliest=earliest,
                    cutoff=cutoff, interval=interval_minutes, now=started,
                    prices=tuple(row[1:5]),
                    base_volume=_number(row[5]) * base_per_contract,
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError(f"OKX has no completed candles: {contract}")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            replace(c, observed_at=observed) for c in candles
        ), interval_minutes, observed)


class KucoinMarketClient(_HttpFeed):
    """KuCoin Futures contract metadata + public UTA V2 futures klines."""

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
            base = "XBT" if asset == "BTC" else asset
            contract = f"{base}USDTM"
            info = await self._get(
                f"https://api-futures.kucoin.com/api/v1/contracts/{contract}", {}
            )
            meta = info.get("data") if isinstance(info, dict) else None
            if (not isinstance(info, dict) or info.get("code") != "200000"
                or not isinstance(meta, dict) or meta.get("symbol") != contract
                or meta.get("baseCurrency") != base
                or meta.get("quoteCurrency") != "USDT"
                or meta.get("settleCurrency") != "USDT"
                or meta.get("status") != "Open"
                or meta.get("expireDate") is not None
                or meta.get("isInverse") is not False):
                raise MarketDataError("KuCoin active linear perpetual unavailable")
            base_per_contract = _number(meta.get("multiplier"), positive=True)
            data = await self._get(
                "https://api.kucoin.com/api/ua/v2/market/kline",
                {"tradeType": "FUTURES", "symbol": contract, "klineType": "TRADE",
                 "interval": _INTERVALS[interval_minutes][1],
                 "startAt": earliest // 1000, "endAt": cutoff // 1000},
            )
            result = data.get("data") if isinstance(data, dict) else None
            if (not isinstance(data, dict) or data.get("code") != "200000"
                or not isinstance(result, dict)
                or result.get("tradeType") != "FUTURES"
                or result.get("symbol") != contract
                or not isinstance(result.get("list"), list)
                or len(result["list"]) > bars + 1):
                raise MarketDataError("invalid KuCoin futures kline response")
            previous = -1
            added = 0
            for row in sorted(result["list"], key=lambda x: str(x[0]) if isinstance(x, list) and x else ""):
                if not isinstance(row, list) or len(row) < 7:
                    raise MarketDataError("invalid KuCoin kline")
                ts = _timestamp(row[0], kucoin=True)
                if ts <= previous:
                    raise MarketDataError("duplicate KuCoin candle timestamp")
                previous = ts
                if ts < earliest:
                    continue
                candle = _make_candle(
                    venue="kucoin", contract=contract, opened=ts,
                    earliest=earliest, cutoff=cutoff, interval=interval_minutes,
                    now=started, prices=tuple(row[1:5]),
                    base_volume=_number(row[5]) * base_per_contract,
                )
                if candle is not None:
                    candles.append(candle)
                    added += 1
            if not added:
                raise MarketDataError(f"KuCoin has no completed candles: {contract}")
        observed = datetime.now(UTC)
        return MarketBatch((), tuple(
            replace(c, observed_at=observed) for c in candles
        ), interval_minutes, observed)
