"""A revised mid-window bar must not fail the whole collector batch.

Live, kucoin-ETH and deribit-ETH/SOL failed every cycle with
CandleConflictError even after the settlement margin: exchanges revise
bars beyond the newest one. Isolation bounds any conflict to that one
bar (stored values kept, rest of the batch lands, feed stays ok).
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
    def __init__(self, venue, scripts):
        self.venue = venue
        self.scripts = scripts
        self.calls = 0

    async def collect(self, *, assets, interval_minutes, bars):
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


@pytest.mark.asyncio
async def test_revised_mid_window_bar_skipped_rest_lands(tmp_path):
    first = [_bar("deribit", BASE + i * timedelta(minutes=5), 100 + i)
             for i in range(4)]
    # Cycle 2 revises a middle bar (not the newest) and adds a newer bar.
    second = [_bar("deribit", BASE + i * timedelta(minutes=5), 100 + i)
              for i in range(4)]
    second[1] = _bar("deribit", BASE + timedelta(minutes=5), 999)
    second.append(_bar("deribit", BASE + timedelta(minutes=20), 104))
    provider = ScriptedProvider("deribit", [first, second])
    store = SQLiteHotStore(tmp_path / "isolation.sqlite")
    collector = PerpetualMarketCollector(
        store=store, assets=("BTC",),
        providers={"deribit": provider},
    )
    try:
        await collector.collect_once()
        await collector.collect_once()
        stored = collector.market.recent_candles(
            venue="deribit", contract="BTC_USDC-PERPETUAL", quote="USDC",
            interval_minutes=5, count=10,
        )
        # The revised middle bar keeps its stored value; the bar held by
        # the margin last cycle lands, and the batch stays healthy.
        assert [row["close_price"] for row in stored] == [100, 101, 102, 103]
        health = collector.market.feed_health()
        assert len(health) == 1 and health[0]["status"] == "ok"
    finally:
        await collector.aclose()
        store.close()
