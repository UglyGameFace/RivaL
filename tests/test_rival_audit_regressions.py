"""Adversarial proof that displayed menu/market data stays trustworthy."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.discord_app import auto_hunt, polymarket_watch
from parlay_bot.discord_app.home import GuidedHome
from parlay_bot.storage.hot import SQLiteHotStore


def test_dust_order_does_not_set_reference_fill_price():
    now = datetime.now(UTC)
    book = {"timestamp": str(int(now.timestamp() * 1000)), "asks": [
        {"price": "0.40", "size": "0.01"},
        {"price": "0.57", "size": "20"},
    ]}
    assert polymarket_watch._best_ask(book, now=now) == .4
    assert polymarket_watch._average_ask(book, now=now) == pytest.approx(
        (.01 * .4 + 9.99 * .57) / 10,
    )
    assert polymarket_watch._average_ask(
        {**book, "asks": book["asks"][:1]}, now=now,
    ) is None


@pytest.mark.asyncio
async def test_qualified_scan_uses_clock_after_slow_await(monkeypatch):
    start = datetime.now(UTC)
    clock = {"at": start}

    class AdvancingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["at"]

    monkeypatch.setattr(auto_hunt, "datetime", AdvancingClock)

    async def late(*a, asset, horizon_minutes, decision_out, **kw):
        if (asset, horizon_minutes) == ("BTC", 5):
            clock["at"] = start + timedelta(seconds=60)
            decision_out.update({
                "is_watch": True, "quote_at": start,
                "target": start + timedelta(minutes=4),
                "issued": start - timedelta(seconds=10),
                "probability": .7, "venue": "hyperliquid",
                "engine": "live", "model_version": "model",
            })

    monkeypatch.setattr(auto_hunt, "signal_embed", late)
    assert await auto_hunt.qualified_watches(
        object(), object(), stream_shadow=None,
    ) == []


@pytest.mark.asyncio
async def test_tier_enable_denied_if_dm_test_fails(tmp_path):
    hot = SQLiteHotStore(tmp_path / "dm-denied.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    user = SimpleNamespace(
        dm_channel=SimpleNamespace(
            send=AsyncMock(side_effect=TimeoutError("blocked")),
        ),
    )
    bot = SimpleNamespace(get_user=lambda uid: user, fetch_user=AsyncMock())
    service = auto_hunt.AutoHuntService(
        bot=bot, market=object(), quotes=object(), store=store,
    )
    try:
        enabled, message = await service.toggle_tier(4001, "qualified")
        assert not enabled and "NOT enabled" in message
        assert not store.enabled("4001")
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_live_buttons_stay_attached_during_slow_render(tmp_path):
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.bot import RivalDiscordClient

    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "guided.sqlite"), discord_token="test-token",
    ))
    home = GuidedHome(
        owner_id=123, location=bot.location_service,
        builder=bot.parlay_builder, collector=bot.current_collector,
        market=None, targets=bot.direction_targets, paper=bot.paper_ledger,
        quotes=bot.public_quotes, profit=bot.quote_profit,
    )

    def interaction():
        return SimpleNamespace(
            user=SimpleNamespace(id=123), response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
                is_done=lambda: True,
            ), edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    started, release = asyncio.Event(), asyncio.Event()

    async def stalled():
        started.set()
        await release.wait()
        return discord.Embed(title="Ready")

    home.render = stalled
    first = interaction()
    second = interaction()
    live_items = list(home.children)
    try:
        job = asyncio.create_task(home.redraw(first))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert all(item.view is home for item in live_items)
        # The old visible button must acknowledge a new tap even if the
        # first request still has a slow render pending.
        button = next(item for item in live_items if item.custom_id == "rival:guided:crypto-desk")
        tap = asyncio.create_task(button.callback(second))
        await asyncio.sleep(0.01)
        second.response.defer.assert_awaited_once()
        assert all(item.view is home for item in live_items)
        release.set()
        await asyncio.wait_for(asyncio.gather(job, tap), timeout=4)
        replacement = first.edit_original_response.await_args.kwargs["view"]
        assert replacement is not home
        assert all(item.view is home for item in live_items)
        assert all(item.view is replacement for item in replacement.children)
        replacement.stop()
    finally:
        release.set()
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
