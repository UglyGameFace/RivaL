"""Research tabs must be usable, show live PostgreSQL data, and cache history only."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from parlay_bot.discord_app.bot import next_collection_delay
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


class Response:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class Interaction:
    def __init__(self):
        self.response = Response()
        self.user = SimpleNamespace(id=21)
        self.embed = None
        self.view = None

    async def edit_original_response(self, *, embed, view):
        self.embed = embed
        self.view = view


def controls(view):
    return {item.custom_id: item for item in view.children}


def test_research_both_tabs_are_clickable_and_refresh_is_visible():
    dashboard = ResearchDashboard(
        owner_id=21, market=None, ai=None,
        venues=("hyperliquid", "dydx"),
    )
    try:
        buttons = controls(dashboard)
        assert buttons["rival:research:math"].style is discord.ButtonStyle.success
        assert buttons["rival:research:ai"].style is discord.ButtonStyle.primary
        assert not buttons["rival:research:math"].disabled
        assert not buttons["rival:research:ai"].disabled
        assert buttons["rival:research:refresh"].style is discord.ButtonStyle.primary
        assert not buttons["rival:research:refresh"].disabled
        dashboard.screen = "ai"
        dashboard._rebuild()
        buttons = controls(dashboard)
        assert buttons["rival:research:math"].style is discord.ButtonStyle.primary
        assert buttons["rival:research:ai"].style is discord.ButtonStyle.success
        assert not buttons["rival:research:ai"].disabled
        assert "rival:research:refresh" in buttons
    finally:
        dashboard.stop()


@pytest.mark.asyncio
async def test_live_health_is_not_cached_but_history_is_cached_until_refresh(
    tmp_path, monkeypatch,
):
    import parlay_bot.discord_app.research_dashboard as module

    store = SQLiteHotStore(tmp_path / "live.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime.now(UTC)
    market.record_feed_health(
        venue="hyperliquid", asset="BTC", status="ok", attempted_at=now,
        new_candles=0, total_candles=0,
        last_candle_close=now - timedelta(minutes=5),
    )
    calls = []

    def expensive_history(_market, *, asset, venues):
        calls.append((asset, venues))
        return {"saved": 42, "expected": 1000, "coverage_pct_ceiling": 4.2}

    monkeypatch.setattr(module, "history_inventory", expensive_history)
    dashboard = ResearchDashboard(
        owner_id=21, market=market, ai=None,
        venues=("hyperliquid", "dydx"),
    )
    try:
        first = await dashboard.render()
        assert "1/2" in next(
            f.value for f in first.fields if f.name == "🟢 Live collection status"
        )
        assert len(calls) == 1
        market.record_feed_health(
            venue="dydx", asset="BTC", status="ok",
            attempted_at=datetime.now(UTC),
            new_candles=0, total_candles=0,
            last_candle_close=datetime.now(UTC) - timedelta(minutes=5),
        )
        second = await dashboard.render()
        assert "2/2" in next(
            f.value for f in second.fields if f.name == "🟢 Live collection status"
        )
        assert len(calls) == 1
        button = controls(dashboard)["rival:research:refresh"]
        interaction = Interaction()
        await button.callback(interaction)
        assert interaction.response.deferred
        assert button.view is None  # old control detached on redraw
        assert interaction.view is dashboard
        assert len(calls) == 2
        assert "2/2" in next(
            f.value for f in interaction.embed.fields
            if f.name == "🟢 Live collection status"
        )
        assert interaction.embed.timestamp is not None
    finally:
        dashboard.stop()
        store.close()


@pytest.mark.asyncio
async def test_api_success_without_completed_candle_is_not_a_fresh_feed(tmp_path):
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime.now(UTC)
    market.record_feed_health(
        venue="hyperliquid", asset="BTC", status="ok", attempted_at=now,
        new_candles=0, total_candles=0,
        last_candle_close=now - timedelta(hours=1),
    )
    view = ResearchDashboard(
        owner_id=21, market=market, ai=None, venues=("hyperliquid",),
    )
    try:
        embed = await view.render()
        health = next(
            f.value for f in embed.fields if f.name == "🟢 Live collection status"
        )
        assert "0/1" in health
    finally:
        view.stop()
        store.close()


def test_perpetual_poll_schedule_stays_on_utc_boundaries_without_drift():
    now = datetime(2026, 10, 28, 13, 5, 30, tzinfo=UTC)
    assert next_collection_delay(now, 300) == pytest.approx(285)
    assert next_collection_delay(
        datetime(2026, 10, 28, 13, 9, 59, tzinfo=UTC), 300,
    ) == pytest.approx(16)
    assert next_collection_delay(
        datetime(2026, 10, 28, 13, 10, 20, tzinfo=UTC), 300,
    ) == pytest.approx(295)
    with pytest.raises(ValueError, match="timezone"):
        next_collection_delay(datetime(2026, 10, 28, 13, 10), 300)
