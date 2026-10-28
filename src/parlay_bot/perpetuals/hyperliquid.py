"""Read-only, bounded Hyperliquid USDC perpetual market-data ingestion.

Candles and asset contexts are observations, NOT source forecast probabilities.
Never manufacture missing/unfinished candles or bypass unavailable endpoints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from parlay_bot.perpetuals.fusion import utc

INFO_URL = "https://api.hyperliquid.xyz/info"
INTERVALS = {5: "5m", 15: "15m", 60: "1h", 240: "4h"}
MAX_BARS = 200


class MarketDataError(RuntimeError):
    """Provider returned an unavailable or untrustworthy market observation."""


def _float(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MarketDataError("invalid market number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("invalid market number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise MarketDataError("nonfinite or nonpositive market number")
    return result


def _nonnegative(value: Any) -> float:
    result = _float(value)
    if result < 0:
        raise MarketDataError("negative market quantity")
    return result


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketDataError("invalid market timestamp or count")
    return value


@dataclass(frozen=True)
class MarketContext:
    venue: str
    contract: str
    quote: str
    observed_at: datetime
    mark_price: float
    oracle_price: float
    funding_rate: float
    open_interest_base: float


@dataclass(frozen=True)
class MarketCandle:
    venue: str
    contract: str
    quote: str
    interval_minutes: int
    opened_at: datetime
    closed_at: datetime
    observed_at: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume_base: float
    trades: int


@dataclass(frozen=True)
class MarketBatch:
    contexts: tuple[MarketContext, ...]
    candles: tuple[MarketCandle, ...]
    interval_minutes: int
    observed_at: datetime


class HyperliquidMarketClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _info(self, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                INFO_URL, json=payload, headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # A blocked/limited provider is an error, not an invitation to bypass.
            raise MarketDataError("public market request failed") from exc

    async def collect(
        self, *, assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        interval_minutes: int = 5, bars: int = 48,
        as_of: datetime | None = None,
    ) -> MarketBatch:
        if interval_minutes not in INTERVALS or not 1 <= bars <= MAX_BARS:
            raise ValueError("invalid interval or history bounds")
        if not assets or len(assets) > 10 or len(assets) != len(set(assets)) or any(
            not a.isascii() or not a.isupper() or not a.isalnum() or len(a) > 15
            for a in assets
        ):
            raise ValueError("use unique simple uppercase perpetual contract symbols")

        observed = utc(as_of if as_of is not None else datetime.now(UTC))
        interval_ms = interval_minutes * 60_000
        # The current, still-forming bucket must never be used for training.
        end_ms = int(observed.timestamp() * 1000) // interval_ms * interval_ms
        start_ms = end_ms - bars * interval_ms
        response = await self._info({"type": "metaAndAssetCtxs", "dex": ""})
        if (
            not isinstance(response, list) or len(response) != 2
            or not isinstance(response[0], dict)
            or not isinstance(response[0].get("universe"), list)
            or not isinstance(response[1], list)
            or len(response[0]["universe"]) != len(response[1])
        ):
            raise MarketDataError("invalid perpetual universe/context alignment")
        available: dict[str, dict[str, Any]] = {}
        for market, context in zip(response[0]["universe"], response[1]):
            if not isinstance(market, dict) or not isinstance(context, dict):
                raise MarketDataError("invalid perpetual market metadata")
            name = market.get("name")
            if not isinstance(name, str) or not name or name in available:
                raise MarketDataError("invalid or duplicate contract identity")
            if not market.get("isDelisted", False):
                available[name] = context

        contexts: list[MarketContext] = []
        candles: list[MarketCandle] = []
        for asset in assets:
            if asset not in available:
                raise MarketDataError(f"active perpetual unavailable: {asset}")
            context = available[asset]
            contexts.append(MarketContext(
                "hyperliquid", asset, "USDC", observed,
                _float(context.get("markPx"), positive=True),
                _float(context.get("oraclePx"), positive=True),
                _float(context.get("funding")),
                _nonnegative(context.get("openInterest")),
            ))
            rows = await self._info({
                "type": "candleSnapshot",
                "req": {"coin": asset, "interval": INTERVALS[interval_minutes],
                        "startTime": start_ms, "endTime": end_ms},
            })
            if not isinstance(rows, list) or len(rows) > bars + 1:
                raise MarketDataError("invalid or unbounded candle response")
            count_before = len(candles)
            previous = -1
            for row in rows:
                if (
                    not isinstance(row, dict) or row.get("s") != asset
                    or row.get("i") != INTERVALS[interval_minutes]
                ):
                    raise MarketDataError("wrong candle contract or interval")
                opened = _integer(row.get("t"))
                closed = _integer(row.get("T"))
                if opened <= previous:
                    raise MarketDataError("duplicate or out-of-order candle")
                previous = opened
                if opened % interval_ms or closed != opened + interval_ms - 1:
                    raise MarketDataError("wrong candle interval boundaries")
                if opened >= end_ms and opened < end_ms + interval_ms:
                    continue  # Provider may return an unfinished candle.
                if opened < start_ms or opened >= end_ms:
                    raise MarketDataError("candle outside requested period")
                o, h, low, c = (
                    _float(row.get(field), positive=True)
                    for field in ("o", "h", "l", "c")
                )
                if low > min(o, c) or h < max(o, c) or h < low:
                    raise MarketDataError("inconsistent OHLC prices")
                candles.append(MarketCandle(
                    "hyperliquid", asset, "USDC", interval_minutes,
                    datetime.fromtimestamp(opened / 1000, UTC),
                    datetime.fromtimestamp(closed / 1000, UTC),
                    observed, o, h, low, c,
                    _nonnegative(row.get("v")), _integer(row.get("n")),
                ))
            if len(candles) == count_before:
                raise MarketDataError(f"no complete candle returned for {asset}")
        return MarketBatch(tuple(contexts), tuple(candles), interval_minutes, observed)
