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
