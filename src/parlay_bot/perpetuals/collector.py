"""Always-on, rate-limited public perpetuals collection for RivaL on Discloud.

Only public read-only APIs; each venue/asset is isolated. Collection starts
on the bot's PostgreSQL deployment and is observable from Discord.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from parlay_bot.perpetuals.additional_venues import (
    BinanceFuturesMarketClient,
    DeribitMarketClient,
)
from parlay_bot.perpetuals.dydx import DydxMarketClient
from parlay_bot.perpetuals.final_venues import LighterMarketClient, PhemexMarketClient
from parlay_bot.perpetuals.hyperliquid import (
    HyperliquidMarketClient,
    MarketBatch,
    MarketDataError,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.next_venues import KucoinMarketClient, OkxMarketClient
from parlay_bot.perpetuals.other_venues import BitgetMarketClient, GateMarketClient
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)

# Do not retry blocked exchange endpoints every five minutes.
_PAUSES = {"blocked": timedelta(hours=6), "rate_limited": timedelta(minutes=20)}


class PublicPerpProvider(Protocol):
    async def collect(
        self, *, assets: tuple[str, ...], interval_minutes: int, bars: int,
    ) -> MarketBatch: ...

    async def aclose(self) -> None: ...


def _failure_kind(error: Exception) -> str:
    if isinstance(error, MarketDataError):
        cause = error.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            if cause.response.status_code in (401, 403, 451):
                return "blocked"
            if cause.response.status_code == 429:
                return "rate_limited"
            return "http_error"
        if isinstance(cause, httpx.TimeoutException):
            return "timeout"
        if isinstance(cause, httpx.HTTPError):
            return "network_error"
        return "invalid_market_data"
    return "storage_error"


class PerpetualMarketCollector:
    """Collect a small overlapping window on every interval, with safe feedback.

    No trading permission, Kraken account, user credentials, or manual trigger
    required. Inject providers to exercise failure/isolation in tests.
    """

    def __init__(
        self,
        *,
        store: RelationalHotStore,
        assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        interval_minutes: int = 5,
        bars: int = 48,
        providers: Mapping[str, PublicPerpProvider] | None = None,
    ) -> None:
        if (
            not assets or len(assets) > 10 or len(assets) != len(set(assets))
            or any(not s.isascii() or not s.isupper() or not s.isalnum() for s in assets)
            or interval_minutes != 5 or not 1 <= bars <= 48
        ):
            raise ValueError("invalid perpetual collection settings")
        self.market = PerpetualMarketStore(store)
        self.assets = assets
        self.interval_minutes = interval_minutes
        self.bars = bars
        self.providers: dict[str, PublicPerpProvider] = (
            dict(providers) if providers is not None
            else {
                "hyperliquid": HyperliquidMarketClient(),
                "dydx": DydxMarketClient(),
                "bitget": BitgetMarketClient(),
                "gate": GateMarketClient(),
                "okx": OkxMarketClient(),
                "kucoin": KucoinMarketClient(),
                "deribit": DeribitMarketClient(),
                "binance": BinanceFuturesMarketClient(),
                "lighter": LighterMarketClient(),
                "phemex": PhemexMarketClient(),
            }
        )
        if not self.providers or any(
            not key or key not in {"hyperliquid", "dydx", "bitget", "gate", "okx", "kucoin",
                        "deribit", "binance", "lighter", "phemex"} for key in self.providers
        ):
            raise ValueError("unsupported public market provider")
        self._pauses: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def collect_once(self, *, now: datetime | None = None) -> None:
        """One bounded cycle. Each asset/provider survives failures of its peers."""
        async with self._lock:
            started = now if now is not None else datetime.now(UTC)
            if started.tzinfo is None or started.utcoffset() is None:
                raise ValueError("clock must be timezone-aware")
            await asyncio.to_thread(self.market.initialize)
            for name, provider in self.providers.items():
                if self._pauses.get(name, datetime.min.replace(tzinfo=UTC)) > started:
                    continue
                for asset in self.assets:
                    try:
                        batch = await provider.collect(
                            assets=(asset,), interval_minutes=self.interval_minutes,
                            bars=self.bars,
                        )
                        if len(batch.candles) == 0 or any(
                            item.venue != name for item in batch.candles
                        ):
                            raise MarketDataError("mismatched or empty market batch")
                        result = await asyncio.to_thread(self.market.ingest, batch)
                        await asyncio.to_thread(
                            self.market.record_feed_health,
                            venue=name, asset=asset, status="ok",
                            attempted_at=datetime.now(UTC),
                            new_candles=result.inserted_candles,
                            total_candles=len(batch.candles),
                            last_candle_close=max(
                                candle.closed_at for candle in batch.candles
                            ),
                        )
                        _LOG.info(
                            "RivaL perp collection OK venue=%s asset=%s bars=%d new=%d",
                            name, asset, len(batch.candles), result.inserted_candles,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # noqa: BLE001 - independent fail-closed feeds
                        kind = _failure_kind(error)
                        await asyncio.to_thread(
                            self.market.record_feed_health,
                            venue=name, asset=asset, status=kind,
                            attempted_at=datetime.now(UTC),
                            new_candles=0, total_candles=0,
                        )
                        _LOG.warning(
                            "RivaL perp collection FAILED venue=%s asset=%s reason=%s",
                            name, asset, kind,
                        )
                        if kind in _PAUSES:
                            self._pauses[name] = datetime.now(UTC) + _PAUSES[kind]
                            _LOG.warning(
                                "RivaL perp venue=%s paused reason=%s",
                                name, kind,
                            )
                            break

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
