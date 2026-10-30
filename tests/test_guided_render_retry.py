"""A timed-out guided render must self-heal exactly once.

Before this, a render that exceeded the 12s cap left a "Still Loading"
embed with no recovery: the user had to tap again. Now the first timeout
schedules one background re-render; the retry itself never retries, and
a retry edit failure is logged only, never user-facing spam.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.home import GuidedHome


def _client(tmp_path):
    return RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "retry.sqlite"), discord_token="<redacted>",
    ))


def _home(bot, owner=1234567):
    return GuidedHome(
        owner_id=owner,
        location=bot.location_service,
        builder=bot.parlay_builder,
        collector=bot.current_collector,
        market=bot.perp_collector.market if bot.perp_collector else None,
        targets=bot.direction_targets, paper=bot.paper_ledger,
        quotes=bot.public_quotes, profit=bot.quote_profit,
    )


def _interaction(owner=1234567):
    return SimpleNamespace(
        id=9000001, user=SimpleNamespace(id=owner),
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _cap_wait_for(monkeypatch):
    real_wait_for = asyncio.wait_for

    async def capped(coro, timeout=None):
        capped_timeout = min(timeout, 0.05) if timeout else timeout
        return await real_wait_for(coro, timeout=capped_timeout)

    monkeypatch.setattr(asyncio, "wait_for", capped)


@pytest.mark.asyncio
async def test_timed_out_render_retries_once_and_stops(tmp_path, monkeypatch):
    bot = _client(tmp_path)
    home = _home(bot)
    interaction = _interaction()
    _cap_wait_for(monkeypatch)
    original_render = home.render
    calls = {"n": 0}

    async def flaky_render():
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(1)  # cancelled by the capped wait_for
        return await original_render()

    home.render = flaky_render
    try:
        await home.redraw(interaction)
        assert interaction.edit_original_response.await_count == 1
        first_embed = interaction.edit_original_response.await_args.kwargs["embed"]
        assert first_embed.title == "⏳ Still Loading"
        await asyncio.sleep(3)
        assert interaction.edit_original_response.await_count == 2
        second_embed = interaction.edit_original_response.await_args.kwargs["embed"]
        assert second_embed.title == "RivaL | Betting Genie"
        interaction.followup.send.assert_not_called()
        # No loop: a further retry window produces no third edit.
        await asyncio.sleep(2.5)
        assert interaction.edit_original_response.await_count == 2
    finally:
        home.stop()


@pytest.mark.asyncio
async def test_retry_edit_failure_is_silent(tmp_path, monkeypatch):
    bot = _client(tmp_path)
    home = _home(bot)
    interaction = _interaction()
    _cap_wait_for(monkeypatch)
    original_render = home.render
    calls = {"n": 0}

    async def flaky_render():
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(1)
        return await original_render()

    home.render = flaky_render
    interaction.edit_original_response.side_effect = [
        None,
        discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom"),
    ]
    try:
        await home.redraw(interaction)
        await asyncio.sleep(3)
        assert interaction.edit_original_response.await_count == 2
        interaction.followup.send.assert_not_called()
    finally:
        home.stop()
