"""Visible calls remain separate from verified trade recommendations."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.discord_app import call_board
from parlay_bot.discord_app.call_board import CallBoardService, live_calls_embed
from parlay_bot.discord_app.targeting import TargetSnapshot
from parlay_bot.storage.hot import SQLiteHotStore


def _snapshot(h, now, *, direction="UP", p=0.62, status="unqualified_model",
              qualified=False):
    if direction == "WAIT":
        return TargetSnapshot(
            asset="BTC", horizon_minutes=h, direction="WAIT",
            detail="No verified prospective prediction", status="no_forecast",
        )
    return TargetSnapshot(
        asset="BTC", horizon_minutes=h, direction=direction,
        detail="Raw research only", venue="hyperliquid", probability=p,
        price=80123.56, issued_at=now-timedelta(seconds=30),
        ends_at=now+timedelta(minutes=h),
        status=status, evidence_qualified=qualified,
    )


def test_call_board_shows_unqualified_up_down_and_only_issued_next(monkeypatch):
    now = datetime(2026, 11, 16, 3, tzinfo=UTC)
    snapshots = {
        5: _snapshot(5, now, direction="DOWN", p=.53, status="weak_model"),
        15: _snapshot(15, now, direction="UP", p=.67),
        60: _snapshot(60, now, direction="WAIT"),
    }
    monkeypatch.setattr(call_board, "target_snapshot",
                        lambda _market, *, asset, horizon_minutes, now:
                        snapshots[horizon_minutes])
    monkeypatch.setattr(call_board, "recent_watches",
                        lambda _market, **_kw: [
                            {"horizon_minutes": 15, "venue": "hyperliquid",
                             "issued": now-timedelta(seconds=25),
                             "target": now+timedelta(minutes=25),
                             "probability_up": .64},
                            {"horizon_minutes": 15, "venue": "hyperliquid",
                             "issued": now+timedelta(seconds=20),
                             "target": now+timedelta(minutes=20),
                             "probability_up": .90},
                            {"horizon_minutes": 5, "venue": "wrong-source",
                             "issued": now-timedelta(seconds=10),
                             "target": now+timedelta(minutes=20),
                             "probability_up": .85},
                        ])
    card, key = live_calls_embed(object(), "BTC", now=now)
    assert len(key) == 64
    assert [x.name for x in card.fields[:3]] == [
        "5-minute call", "15-minute call", "60-minute call",
    ]
    assert "DOWN" in card.fields[0].value
    assert "UP" in card.fields[1].value
    assert "UNQUALIFIED RESEARCH; NO TRADE CALL" in card.fields[1].value
    assert "64%" not in card.fields[1].value or "Next" in card.fields[1].value
    assert "Next *already-issued* target" in card.fields[1].value
    assert "90%" not in card.fields[1].value
    assert "Next issued target: not yet available" in card.fields[0].value
    assert "NO CURRENT FORECAST" in card.fields[2].value
    assert "Not verified" in card.fields[3].value
    assert "No real orders" in card.footer.text


def test_call_board_keeps_all_horizons_as_wait_when_collector_unavailable():
    card, first = live_calls_embed(None, "XRP",
                                   now=datetime(2026, 11, 16, tzinfo=UTC))
    assert sum("NO CURRENT FORECAST" in x.value for x in card.fields) == 3
    assert all("UP" not in x.value for x in card.fields[:3])
    assert len(first) == 64


@pytest.mark.asyncio
async def test_live_calls_are_opt_in_channel_posts_and_edit_in_place(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "calls.sqlite")
    try:
        state = {"hash": "a"}
        def render(_market, asset):
            return discord.Embed(title=f"Live {asset}"), state["hash"]
        monkeypatch.setattr(call_board, "live_calls_embed", render)
        original = SimpleNamespace(id=321, edit=AsyncMock())
        sent = SimpleNamespace(id=321, delete=AsyncMock())
        channel = SimpleNamespace(
            id=777, send=AsyncMock(return_value=sent),
            get_partial_message=lambda _id: original,
        )
        bot = SimpleNamespace(get_partial_messageable=lambda _id: channel)
        service = CallBoardService(bot, store, object())
        interaction = SimpleNamespace(
            channel=channel, guild_id=12345, user=SimpleNamespace(id=42),
        )
        result = await service.start(interaction, "BTC")
        assert "One message updates automatically" in result
        assert len(service._rows()) == 1
        assert channel.send.await_count == 1
        assert "already has" in await service.start(interaction, "BTC")
        assert channel.send.await_count == 1

        assert await service.cycle() == 0
        state["hash"] = "b"
        assert await service.cycle() == 1
        assert original.edit.await_count == 1
        assert service._rows()[0]["fingerprint"] == "b"
        assert await service.stop_owner(999) is False
        assert len(service._rows()) == 1
        assert await service.stop_owner(42) is True
        assert not service._rows()
        assert original.edit.await_count == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_calls_reject_dms_and_are_revoked_by_stop_all(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "calls-stop.sqlite")
    try:
        monkeypatch.setattr(call_board, "live_calls_embed",
                            lambda _m, asset: (discord.Embed(title=asset), "hash"))
        original = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(
            id=777, send=AsyncMock(return_value=SimpleNamespace(id=321)),
            get_partial_message=lambda _id: original,
        )
        service = CallBoardService(
            SimpleNamespace(get_partial_messageable=lambda _id: channel),
            store, object(),
        )
        user = SimpleNamespace(id=42)
        dm = SimpleNamespace(channel=channel, guild_id=None, user=user)
        assert "No DM" in await service.start(dm, "ETH")
        channel.send.assert_not_awaited()
        await service.start(SimpleNamespace(
            channel=channel, guild_id=111, user=user,
        ), "ETH")
        from parlay_bot.discord_app.alert_control import stop_all_notifications
        message = await stop_all_notifications(
            42, auto_hunt=None, targets=None, tracker=None, calls=service,
        )
        assert "Stopped all" in message
        assert not service._rows()
    finally:
        store.close()
