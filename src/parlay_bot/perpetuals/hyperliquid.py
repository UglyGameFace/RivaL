"""Bounded, unauthenticated Hyperliquid perpetual-candle ingestion.

Official read-only endpoint: POST https://api.hyperliquid.xyz/info.
No client in this module has exchange trading credentials or order methods.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.fusion import utc

INFO_URL = "https://api.hyperliquid.xyz/info"
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
# Limit each request: candleSnapshot has a base weight plus weight per 60 rows.
MAX_CANDLES = 180
ASSETS = frozenset({"BTC", "ETH", "SOL"})


@dataclass(frozen=True)
class PerpCandle:
    venue: str
    asset: str
    quote: str
    contract_kind: str
    interval: str
    opened_at: datetime
    closed_at: datetime
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    trades: int

    def __post_init__(self) -> None:
        if (self.venue, self.quote, self.contract_kind) != (
            "hyperliquid", "USDC", "linear_perpetual"
        ):
            raise ValueError("unexpected market identity")
        if self.asset not in ASSETS or self.interval not in INTERVALS:
            raise ValueError("unsupported candle instrument or interval")
        opened = utc(self.opened_at)
        closed = utc(self.closed_at)
        duration = timedelta(minutes=INTERVALS[self.interval])
        if closed != opened + duration - timedelta(milliseconds=1):
            raise ValueError("invalid candle boundaries")
        if not all(math.isfinite(x) for x in (
            self.open, self.high, self.low, self.close, self.base_volume
        )):
            raise ValueError("nonfinite candle value")
        if (
            min(self.open, self.high, self.low, self.close) <= 0
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or self.high < self.low
            or self.base_volume < 0
            or type(self.trades) is not int
            or self.trades < 0
        ):
            raise ValueError("invalid OHLCV or trade count")


def _number(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("numeric exchange field required")
    try:
        result = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid exchange numeric field") from exc
    if not math.isfinite(result):
        raise ValueError("exchange numeric field must be finite")
    return result


def normalize_candle(
    raw: dict[str, Any], *, coin: str, interval: str, requested_at: datetime
) -> PerpCandle | None:
    """Only complete, internally valid candles may enter the training store."""
    if not isinstance(raw, dict):
        raise ValueError("candle response row must be an object")
    if raw.get("s") != coin or raw.get("i") != interval:
        raise ValueError("exchange candle identity mismatch")
    start_ms, close_ms = raw.get("t"), raw.get("T")
    if (
        type(start_ms) is not int
        or type(close_ms) is not int
        or start_ms < 0
    ):
        raise ValueError("invalid millisecond timestamps")
    # The API's T is the inclusive last millisecond of the candle.
    if close_ms != start_ms + INTERVALS[interval] * 60_000 - 1:
        raise ValueError("invalid timestamp range")
    end_ms = int(utc(requested_at).timestamp() * 1000)
    if close_ms >= end_ms:
        return None  # Never train on the live, unfinished bar.
    if start_ms % (INTERVALS[interval] * 60_000) != 0:
        raise ValueError("unaligned candle start")
    return PerpCandle(
        venue="hyperliquid", asset=coin, quote="USDC",
        contract_kind="linear_perpetual", interval=interval,
        opened_at=datetime.fromtimestamp(start_ms / 1000, tz=UTC),
        closed_at=datetime.fromtimestamp(close_ms / 1000, tz=UTC),
        open=_number(raw.get("o")), high=_number(raw.get("h")),
        low=_number(raw.get("l")), close=_number(raw.get("c")),
        base_volume=_number(raw.get("v")), trades=raw.get("n"),
    )


class HyperliquidPublicCandles:
    """Read-only API adapter; caller owns the supplied HTTP client if given."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def fetch_closed(
        self, *,
        coin: str,
        interval: str = "5m",
        count: int = 48,
        requested_at: datetime | None = None,
    ) -> tuple[PerpCandle, ...]:
        if coin not in ASSETS or interval not in INTERVALS:
            raise ValueError("unsupported Hyperliquid instrument or interval")
        if type(count) is not int or not 1 <= count <= MAX_CANDLES:
            raise ValueError("count outside bounded free API request window")
        now = utc(requested_at or datetime.now(UTC))
        if now > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("request cutoff cannot be in the future")
        finish = int(now.timestamp() * 1000)
        start = finish - (count + 1) * INTERVALS[interval] * 60_000
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start,
                "endTime": finish,
            },
        }
        if self._client is None:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                headers={"User-Agent": "RivaL-public-perpetual-data/1.0"},
            ) as client:
                response = await client.post(INFO_URL, json=payload)
        else:
            response = await self._client.post(INFO_URL, json=payload)
        response.raise_for_status()  # 403/429 are failures, never fabricated success.
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("unexpected candle response schema")
        result: list[PerpCandle] = []
        seen: set[datetime] = set()
        for raw in data:
            candle = normalize_candle(raw, coin=coin, interval=interval, requested_at=now)
            if candle is None:
                continue
            if candle.opened_at in seen:
                raise ValueError("duplicate exchange candle timestamp")
            seen.add(candle.opened_at)
            if start <= int(candle.opened_at.timestamp() * 1000) <= finish:
                result.append(candle)
        return tuple(sorted(result, key=lambda c: c.opened_at)[-count:])
