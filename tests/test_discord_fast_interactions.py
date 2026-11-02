"""Discord acknowledges before IO and never paints a stale forecast."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient


def _bot(tmp_path):
    return RivalDiscordClient(Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "fast-discord.sqlite"),
        discord_token=None,
    ))


async def _close(bot):
    await bot.zip_resolver.aclose()
    await bot.public_quotes.aclose()
    bot.store.close()


@pytest.mark.asyncio
async def test_customize_alerts_acks_before_first_database_read(tmp_path):
    bot = _bot(tmp_path)
    home = bot._build_guided_home(123)
    sequence = []

    def preferences(_):
        assert sequence == ["ack"]
        sequence.append("db")
        return {
            "assets": ("BTC", "ETH", "SOL"), "horizons": (5, 15),
            "side": "both", "max_ask_pct": 100,
            "min_remaining_seconds": 90,
            "live_shifts": False, "direction_changes": False,
        }

    async def ack(**kwargs):
        assert kwargs["ephemeral"]
        sequence.append("ack")

    home.auto_hunt = SimpleNamespace(
        store=SimpleNamespace(preferences=preferences),
    )
    event = SimpleNamespace(
        user=SimpleNamespace(id=123),
        response=SimpleNamespace(defer=AsyncMock(side_effect=ack)),
        edit_original_response=AsyncMock(),
    )
    try:
        await home.act(event, "hunt-config")
        assert sequence == ["ack", "db"]
        event.edit_original_response.assert_awaited_once()
    finally:
        await _close(bot)


@pytest.mark.asyncio
async def test_old_background_render_cannot_overwrite_newer_home_page(tmp_path):
    bot = _bot(tmp_path)
    home = bot._build_guided_home(123)
    event = SimpleNamespace(edit_original_response=AsyncMock())
    embed = discord.Embed(title="Past market snapshot")
    home.render = AsyncMock(return_value=embed)
    try:
        home._render_revision["value"] = 3
        await home._finish_forecast(event, revision=2)
        event.edit_original_response.assert_not_awaited()
        await home._finish_forecast(event, revision=3)
        event.edit_original_response.assert_awaited_once()
    finally:
        await _close(bot)


@pytest.mark.asyncio
async def test_real_forecast_path_returns_loading_before_slow_research(
    tmp_path, monkeypatch,
):
    bot = _bot(tmp_path)
    home = bot._build_guided_home(123)
    home.navigate("forecast")
    market_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_render():
        market_started.set()
        await release.wait()
        return discord.Embed(title="Verified fresh result")

    class RealInteraction:
        def __init__(self):
            self.edit_original_response = AsyncMock()

    interaction = RealInteraction()
    # Real discord.Interaction is C-defined and can't be instantiated in
    # these tests. Replace the runtime type gate only within this test.
    from parlay_bot.discord_app import home as home_module

    monkeypatch.setattr(home_module.discord, "Interaction", RealInteraction)
    home.render = slow_render
    try:
        await asyncio.wait_for(home.redraw(interaction), timeout=.5)
        call = interaction.edit_original_response.await_args
        assert call.kwargs["embed"].title.startswith("🔄 Checking")
        await asyncio.wait_for(market_started.wait(), timeout=.5)
        release.set()
        await asyncio.sleep(.1)
        assert interaction.edit_original_response.await_count == 2
        assert interaction.edit_original_response.await_args.kwargs[
            "embed"
        ].title == "Verified fresh result"
    finally:
        release.set()
        await _close(bot)


@pytest.mark.asyncio
async def test_open_live_menu_updates_continuously_then_yields_to_navigation(tmp_path, monkeypatch):
    from parlay_bot.discord_app import home as module

    bot = _bot(tmp_path)
    home = bot._build_guided_home(123)
    home.navigate("live")
    home._render_revision["value"] = 1
    edits = SimpleNamespace(edit_original_response=AsyncMock())
    count = 0

    async def ready(*args):
        nonlocal count
        count += 1
        if count == 3:
            home._render_revision["value"] = 2  # a newer Home/coin/source action wins

    home.render = AsyncMock(side_effect=[discord.Embed(title="Live one"), discord.Embed(title="Live two")])
    service = SimpleNamespace(wait_ready=AsyncMock(side_effect=ready))
    original_sleep = asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", lambda delay: original_sleep(0))
    try:
        await home._finish_live_preview(edits, 1, service)
        assert [c.kwargs["embed"].title for c in edits.edit_original_response.await_args_list] == [
            "Live one", "Live two",
        ]
        assert service.wait_ready.await_count == 3
    finally:
        home.stop()
        await _close(bot)


@pytest.mark.asyncio
async def test_contract_ai_wiring_survives_disabled_exchange_collection(tmp_path):
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite", discord_token=None,
        rival_db_path=str(tmp_path / "polymarket-only.sqlite"),
        rival_perp_collection_enabled=False, open_router_api_key="test-key",
    ))
    try:
        assert bot.perp_collector is None
        assert bot.polymarket_research.ai is bot.gemini_research
        assert bot.polymarket_research.ai.available
        assert bot.polymarket_board.research is bot.polymarket_research
        assert bot.auto_hunt.contract_research is bot.polymarket_research
    finally:
        await bot.gemini_research.aclose()
        await _close(bot)


@pytest.mark.asyncio
async def test_research_navigation_acknowledges_and_shows_loading_before_slow_results(
    monkeypatch,
):
    from parlay_bot.discord_app import research_dashboard as research

    view = research.ResearchDashboard(
        owner_id=123, market=None, ai=None, venues=(),
    )
    view.screen = "accuracy"
    view.section = "overview"
    started, release = asyncio.Event(), asyncio.Event()

    class RealInteraction:
        def __init__(self):
            self.response = SimpleNamespace(defer=AsyncMock())
            self.edit_original_response = AsyncMock()

    event = RealInteraction()
    monkeypatch.setattr(research.discord, "Interaction", RealInteraction)

    async def slow_render(self):
        started.set()
        await release.wait()
        return discord.Embed(title="Verified results")

    monkeypatch.setattr(research.ResearchDashboard, "render", slow_render)
    try:
        await asyncio.wait_for(view.redraw(event), timeout=0.5)
        event.response.defer.assert_awaited_once()
        assert event.edit_original_response.await_count == 1
        assert "Checking saved market data" in (
            event.edit_original_response.await_args.kwargs["embed"].description
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        release.set()
        await asyncio.wait_for(view._navigation_state["pending"], timeout=0.5)
        assert event.edit_original_response.await_count == 2
        assert event.edit_original_response.await_args.kwargs["embed"].title == "Verified results"
        assert event.edit_original_response.await_args.kwargs["view"] is not view
    finally:
        release.set()
        view.stop()


@pytest.mark.asyncio
async def test_research_newer_selection_cancels_old_slow_render(monkeypatch):
    from parlay_bot.discord_app import research_dashboard as research

    view = research.ResearchDashboard(owner_id=123, market=None, ai=None, venues=())
    release = asyncio.Event()

    class RealInteraction:
        def __init__(self):
            self.response = SimpleNamespace(defer=AsyncMock())
            self.edit_original_response = AsyncMock()

    monkeypatch.setattr(research.discord, "Interaction", RealInteraction)
    async def controlled_render(self):
        if self.section == "pulse":
            await release.wait()
        return discord.Embed(title=self.section)

    monkeypatch.setattr(research.ResearchDashboard, "render", controlled_render)
    first, second = RealInteraction(), RealInteraction()
    try:
        view.screen, view.section = "math", "pulse"
        await view.redraw(first)
        await asyncio.sleep(0)
        view.section = "spikes"
        await view.redraw(second)
        await asyncio.wait_for(view._navigation_state["pending"], timeout=0.5)
        release.set()
        await asyncio.sleep(0)
        assert second.edit_original_response.await_args.kwargs["embed"].title == "spikes"
        assert first.edit_original_response.await_count == 1  # loading only
    finally:
        release.set()
        view.stop()


@pytest.mark.asyncio
async def test_performance_menu_shows_loading_before_blocked_database(monkeypatch, tmp_path):
    from parlay_bot.discord_app import home as home_module

    bot = _bot(tmp_path)
    home = bot._build_guided_home(123)
    home.navigate("performance")
    started, release = asyncio.Event(), asyncio.Event()

    class RealInteraction:
        def __init__(self):
            self.edit_original_response = AsyncMock()

    monkeypatch.setattr(home_module.discord, "Interaction", RealInteraction)
    interaction = RealInteraction()

    async def slow_render():
        started.set()
        await release.wait()
        return discord.Embed(title="Measured performance")

    home.render = slow_render
    try:
        await asyncio.wait_for(home.redraw(interaction), timeout=0.5)
        assert "Loading BTC performance" in (
            interaction.edit_original_response.await_args.kwargs["embed"].title
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        release.set()
        await asyncio.sleep(0.1)
        assert interaction.edit_original_response.await_count == 2
        assert interaction.edit_original_response.await_args.kwargs[
            "embed"
        ].title == "Measured performance"
    finally:
        release.set()
        home.stop()
        await _close(bot)
