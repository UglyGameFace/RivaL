"""Regression coverage for background perpetual collection and reporting."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.collector import PerpetualMarketCollector
from parlay_bot.perpetuals.hyperliquid import (
    MarketBatch,
    MarketCandle,
    MarketDataError,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 11, 0, tzinfo=UTC)


class DummyProvider:
    def __init__(self, venue: str, *, failure: str | None = None) -> None:
        self.venue = venue
        self.failure = failure
        self.called: list[str] = []
        self.closed = False

    async def collect(
        self, *, assets: tuple[str, ...], interval_minutes: int, bars: int,
    ) -> MarketBatch:
        assert interval_minutes == 5 and bars == 2
        asset = assets[0]
        self.called.append(asset)
        if self.failure == asset:
            raise MarketDataError("unsupported market")
        observed = datetime.now(UTC)
        opened = NOW - timedelta(minutes=5)
        candle = MarketCandle(
            venue=self.venue, contract=asset if self.venue == "hyperliquid" else
            f"{asset}-USD", quote="USDC" if self.venue == "hyperliquid" else "USD",
            interval_minutes=5, opened_at=opened,
            closed_at=NOW - timedelta(milliseconds=1),
            observed_at=observed,
            open_price=100, high_price=110, low_price=90,
            close_price=105, volume_base=12, trades=4,
        )
        return MarketBatch((), (candle,), 5, observed)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_background_collection_persists_two_venues_across_replays(tmp_path, caplog):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    hl = DummyProvider("hyperliquid")
    dx = DummyProvider("dydx")
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC", "ETH"), bars=2,
        providers={"hyperliquid": hl, "dydx": dx},
    )
    try:
        with caplog.at_level(logging.INFO):
            await collector.collect_once()
            await collector.collect_once()
        rows = collector.market.feed_health()
        assert len(rows) == 4
        assert all(row["status"] == "ok" for row in rows)
        assert all(row["total_new_candles"] == 1 for row in rows)
        assert all(row["last_new_candles"] == 0 for row in rows)
        assert all(row["last_success_at"] for row in rows)
        assert all(row["last_candle_close"] == (NOW - timedelta(milliseconds=1)).isoformat() for row in rows)
        assert hl.called == ["BTC", "ETH", "BTC", "ETH"]
        assert dx.called == ["BTC", "ETH", "BTC", "ETH"]
        assert "RivaL perp collection OK" in caplog.text
    finally:
        await collector.aclose()
        store.close()
    assert hl.closed and dx.closed


@pytest.mark.asyncio
async def test_bad_asset_does_not_stop_other_assets_or_venue(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    hl = DummyProvider("hyperliquid", failure="ETH")
    dx = DummyProvider("dydx")
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC", "ETH", "SOL"), bars=2,
        providers={"hyperliquid": hl, "dydx": dx},
    )
    try:
        await collector.collect_once()
        assert hl.called == ["BTC", "ETH", "SOL"]
        assert dx.called == ["BTC", "ETH", "SOL"]
        status = {(x["venue"], x["asset"]): x for x in collector.market.feed_health()}
        assert status[("hyperliquid", "ETH")]["status"] == "invalid_market_data"
        assert status[("hyperliquid", "ETH")]["last_success_at"] is None
        assert status[("hyperliquid", "SOL")]["status"] == "ok"
        assert status[("dydx", "ETH")]["status"] == "ok"
    finally:
        await collector.aclose()
        store.close()


@pytest.mark.asyncio
async def test_geo_block_pauses_venue_not_another(tmp_path):
    class Blocked(DummyProvider):
        async def collect(self, *, assets, interval_minutes, bars):
            self.called.append(assets[0])
            request = httpx.Request("GET", "https://example.invalid/")
            response = httpx.Response(403, request=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MarketDataError("blocked public market") from exc

    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    hl = Blocked("hyperliquid")
    dx = DummyProvider("dydx")
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC", "ETH"), bars=2,
        providers={"hyperliquid": hl, "dydx": dx},
    )
    try:
        await collector.collect_once()
        await collector.collect_once()
        assert hl.called == ["BTC"]
        assert dx.called == ["BTC", "ETH", "BTC", "ETH"]
        health = collector.market.feed_health()
        assert any(
            x["venue"] == "hyperliquid" and x["status"] == "blocked"
            for x in health
        )
    finally:
        await collector.aclose()
        store.close()


@pytest.mark.asyncio
async def test_cancelled_collection_is_not_swallowed(tmp_path):
    class Cancelled(DummyProvider):
        async def collect(self, *, assets, interval_minutes, bars):
            raise asyncio.CancelledError

    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC",), bars=2,
        providers={"dydx": Cancelled("dydx")},
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await collector.collect_once()
    finally:
        await collector.aclose()
        store.close()


def test_bad_collection_config_does_not_start(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    with pytest.raises(ValueError):
        PerpetualMarketCollector(store=store, assets=("BTC", "BTC"))
    with pytest.raises(ValueError):
        PerpetualMarketCollector(store=store, assets=("BTC",), bars=400)
