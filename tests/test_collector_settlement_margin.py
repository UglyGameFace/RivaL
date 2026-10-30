"""Collector settlement margin: a just-closed bar may still be revised.

A bar stored prematurely and then revised by the exchange used to
reject the whole asset batch every cycle until the bar aged out of the
48-bar window (the observed kucoin-ETH / deribit-ETH+SOL storage_error
pattern). The margin is the first layer: holding back the single newest
bar per batch means a revised bar is first stored once settled, so a
revision can never conflict with a premature row. The second layer is
ingest conflict isolation (see test_collector_conflict_isolation.py):
any conflict that still occurs is skipped per bar instead of failing
the batch.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.collector import PerpetualMarketCollector
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.storage.hot import SQLiteHotStore

BASE = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def _bar(venue, opened, close, *, contract="BTC_USDC-PERPETUAL"):
    observed = datetime.now(UTC)
    return MarketCandle(
        venue=venue, contract=contract, quote="USDC",
        interval_minutes=5, opened_at=opened,
        closed_at=opened + timedelta(minutes=5) - timedelta(milliseconds=1),
        observed_at=observed,
        open_price=close, high_price=close + 2, low_price=close - 2,
        close_price=close, volume_base=10.0, trades=-1,
    )


class ScriptedProvider:
    """Returns one scripted bar set per collect call, per asset."""

    def __init__(self, venue, scripts):
        self.venue = venue
        self.scripts = scripts
        self.calls = 0

    async def collect(self, *, assets, interval_minutes, bars):
        assert assets == ("BTC",) and interval_minutes == 5
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        observed = datetime.now(UTC)
        candles = tuple(
            MarketCandle(
                venue=c.venue, contract=c.contract, quote=c.quote,
                interval_minutes=c.interval_minutes, opened_at=c.opened_at,
                closed_at=c.closed_at, observed_at=observed,
                open_price=c.open_price, high_price=c.high_price,
                low_price=c.low_price, close_price=c.close_price,
                volume_base=c.volume_base, trades=c.trades,
            )
            for c in script
        )
        return MarketBatch((), candles, 5, observed)

    async def aclose(self) -> None:
        pass


def _stored(collector):
    return collector.market.recent_candles(
        venue="deribit", contract="BTC_USDC-PERPETUAL", quote="USDC",
        interval_minutes=5, count=10,
    )


@pytest.mark.asyncio
async def test_newest_bar_is_held_back_one_cycle(tmp_path):
    store = SQLiteHotStore(tmp_path / "margin.sqlite")
    bars = [_bar("deribit", BASE + i * timedelta(minutes=5), 100 + i)
            for i in range(3)]
    provider = ScriptedProvider("deribit", [bars])
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC",),
        providers={"deribit": provider},
    )
    try:
        await collector.collect_once()
        stored = _stored(collector)
        assert [row["close_price"] for row in stored] == [100, 101]
        health = collector.market.feed_health()
        assert len(health) == 1 and health[0]["status"] == "ok"
        assert health[0]["total_new_candles"] == 2
    finally:
        await collector.aclose()
        store.close()


@pytest.mark.asyncio
async def test_revised_newest_bar_cannot_conflict(tmp_path):
    store = SQLiteHotStore(tmp_path / "margin-revise.sqlite")
    first = [_bar("deribit", BASE + i * timedelta(minutes=5), 100 + i)
             for i in range(3)]
    # The newest bar of cycle 1 is revised before cycle 2, and a newer
    # bar arrives. Without the margin, cycle 1 would store the premature
    # values and cycle 2 would raise CandleConflictError.
    revised = [_bar("deribit", BASE + i * timedelta(minutes=5), 100 + i)
               for i in range(3)]
    revised[2] = _bar("deribit", BASE + timedelta(minutes=10), 999)
    second = revised + [_bar("deribit", BASE + timedelta(minutes=15), 103)]
    provider = ScriptedProvider("deribit", [first, second])
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC",),
        providers={"deribit": provider},
    )
    try:
        await collector.collect_once()
        await collector.collect_once()
        stored = _stored(collector)
        assert [row["close_price"] for row in stored] == [100, 101, 999]
        health = collector.market.feed_health()
        assert health[0]["status"] == "ok"
    finally:
        await collector.aclose()
        store.close()


@pytest.mark.asyncio
async def test_single_bar_batch_is_kept_whole(tmp_path):
    store = SQLiteHotStore(tmp_path / "margin-single.sqlite")
    provider = ScriptedProvider("deribit", [[_bar("deribit", BASE, 100)]])
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC",),
        providers={"deribit": provider},
    )
    try:
        await collector.collect_once()
        stored = _stored(collector)
        assert [row["close_price"] for row in stored] == [100]
        assert collector.market.feed_health()[0]["status"] == "ok"
    finally:
        await collector.aclose()
        store.close()
