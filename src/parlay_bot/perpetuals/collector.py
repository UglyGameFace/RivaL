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
    DeribitMarketClient,
)
from parlay_bot.perpetuals.dydx import DydxMarketClient
from parlay_bot.perpetuals.final_venues import LighterMarketClient, PhemexMarketClient
from parlay_bot.perpetuals.fusion import utc
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
_PAUSES = {"blocked": timedelta(days=7), "rate_limited": timedelta(minutes=20)}
_PROVIDER_TIMEOUT_SECONDS = 12  # Cap each network operation; no source stalls a full candle.

# A completed candle is trusted once it closed this long before the batch
# was observed. The collector already runs 15s past each candle boundary,
# so this binds only on off-schedule runs; it never shifts the forecast
# target back an interval the way the old one-bar holdback did.
CANDLE_SETTLEMENT_MARGIN = timedelta(seconds=10)


def _retain_settled_candles(batch: MarketBatch) -> MarketBatch:
    """Drop candles too fresh to trust; never invent or shift a target.

    Unfinished candles (closed_at after observed_at) and candles closed
    within the margin are withheld for the next cycle, when they are old
    enough to qualify. Single-bar batches pass through whole.
    """
    if len(batch.candles) <= 1:
        return batch
    observed = utc(batch.observed_at)
    settled = tuple(
        candle for candle in batch.candles
        if utc(candle.closed_at) + CANDLE_SETTLEMENT_MARGIN <= observed
    )
    if not settled:
        raise MarketDataError("No completed candles past settlement margin")
    if len(settled) == len(batch.candles):
        return batch
    return MarketBatch(
        batch.contexts, settled, batch.interval_minutes, batch.observed_at,
    )


class PublicPerpProvider(Protocol):
    async def collect(
        self, *, assets: tuple[str, ...], interval_minutes: int, bars: int,
    ) -> MarketBatch: ...

    async def aclose(self) -> None: ...


def _failure_kind(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
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
                "lighter": LighterMarketClient(),
                "phemex": PhemexMarketClient(),
            }
        )
        if not self.providers or any(
            not key or key not in {"hyperliquid", "dydx", "bitget", "gate", "okx", "kucoin",
                        "deribit", "binance", "lighter", "phemex"} for key in self.providers
        ):
            raise ValueError("unsupported public market provider")
        # Region-blocked Binance Futures is not a default live source. An
        # explicitly injected provider remains available for controlled tests.
        self._pauses: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def collect_once(self, *, now: datetime | None = None) -> None:
        """Collect independent venues concurrently, persist their results serially.

        One slow or blocked exchange must not hold up nine healthy exchanges.
        Per-venue assets remain sequential to avoid a burst against one API;
        PostgreSQL/SQLite writes remain serialized for integrity.
        """
        async with self._lock:
            started = now if now is not None else datetime.now(UTC)
            if started.tzinfo is None or started.utcoffset() is None:
                raise ValueError("clock must be timezone-aware")
            await asyncio.to_thread(self.market.initialize)
            slots = asyncio.Semaphore(4)

            async def fetch_venue(
                name: str, provider: PublicPerpProvider,
            ) -> tuple[str, list[tuple[str, MarketBatch | None, Exception | None]]]:
                outcomes: list[tuple[str, MarketBatch | None, Exception | None]] = []
                async with slots:
                    for asset in self.assets:
                        try:
                            batch = await asyncio.wait_for(
                                provider.collect(
                                    assets=(asset,), interval_minutes=self.interval_minutes,
                                    bars=self.bars,
                                ),
                                timeout=_PROVIDER_TIMEOUT_SECONDS,
                            )
                            if not batch.candles or any(
                                candle.venue != name for candle in batch.candles
                            ):
                                raise MarketDataError("mismatched or empty market batch")
                            outcomes.append((asset, batch, None))
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:  # noqa: BLE001 - isolated source
                            outcomes.append((asset, None, error))
                            if _failure_kind(error) in _PAUSES:
                                break  # Do not keep querying a blocked venue.
                return name, outcomes

            # All network work completes before DB writes. Different venues
            # overlap, but not the requests to the same venue.
            gathered = await asyncio.gather(*(
                fetch_venue(name, provider)
                for name, provider in self.providers.items()
                if self._pauses.get(
                    name, datetime.min.replace(tzinfo=UTC),
                ) <= started
            ))
            for name, outcomes in gathered:
                for asset, batch, fetch_error in outcomes:
                    try:
                        if fetch_error is not None:
                            raise fetch_error
                        assert batch is not None
                        # Settlement rule: keep completed candles that
                        # closed at least CANDLE_SETTLEMENT_MARGIN before
                        # the batch was observed. The previous one-bar
                        # holdback systematically expired every 5m
                        # forecast: holding the newest completed candle
                        # pushed the forecast target one interval back,
                        # so issued_at always landed past target_open +
                        # 5min for horizon 5 (expired=27, issued=27 per
                        # cycle: all 5m expired, only 15m issued).
                        # Unfinished candles (closed_at in the future)
                        # never pass the margin. Single-bar batches are
                        # kept whole so a thin venue can never starve;
                        # any later revision is caught by per-bar
                        # conflict isolation in ingest.
                        batch = _retain_settled_candles(batch)
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
                    except Exception as error:  # noqa: BLE001 - independent feeds
                        kind = _failure_kind(error)
                        try:
                            await asyncio.to_thread(
                                self.market.record_feed_health,
                                venue=name, asset=asset, status=kind,
                                attempted_at=datetime.now(UTC),
                                new_candles=0, total_candles=0,
                            )
                        except Exception:  # noqa: BLE001 - preserve other feeds
                            _LOG.exception(
                                "RivaL source health persistence failed venue=%s asset=%s",
                                name, asset,
                            )
                        _LOG.warning(
                            "RivaL perp collection FAILED venue=%s asset=%s reason=%s error=%s",
                            name, asset, kind, type(error).__name__,
                        )
                        if kind in _PAUSES:
                            self._pauses[name] = datetime.now(UTC) + _PAUSES[kind]
                            _LOG.warning(
                                "RivaL perp venue=%s paused reason=%s",
                                name, kind,
                            )

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
