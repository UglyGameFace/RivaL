"""A mid-contract change of RivaL's Polymarket call reaches the user as a new DM."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.polymarket_live_board import (
    MAX_CHANGE_ALERTS,
    LivePolymarketBoardService,
    LivePolymarketTracker,
)
from parlay_bot.discord_app.polymarket_watch import (
    PolymarketListedWindow,
    PolymarketWindowBoard,
)
from parlay_bot.perpetuals.polymarket_research import ContractCall, PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore


def _call(slug, closes, direction, issued):
    return ContractCall(slug=slug, asset="BTC", horizon=5, issued_at=issued, expires=closes,
                        direction=direction, model="book", thesis="t", risk="r",
                        evidence_sha256="a" * 64, kind="model")


def test_research_records_each_direction_change(tmp_path):
    research = PolymarketResearch(SQLiteHotStore(tmp_path / "r.sqlite"))
    now = datetime.now(UTC)
    closes = now + timedelta(minutes=3)
    first = _call("s", closes, "DOWN", now - timedelta(seconds=30))
    research._math_calls["s"] = first
    research._math_calls["s"] = _call("s", closes, "DOWN", now - timedelta(seconds=15))
    research._note_change("s", first)          # same direction: not a change
    assert research.changes("s") == []
    before = research._math_calls["s"]
    research._math_calls["s"] = _call("s", closes, "UP", now)
    research._note_change("s", before)
    assert [(old, new) for _, old, new in research.changes("s")] == [("DOWN", "UP")]


@pytest.mark.asyncio
async def test_tracker_gets_a_new_dm_for_each_flip_up_to_the_cap():
    now = datetime.now(UTC)
    opens = now - timedelta(minutes=2)
    closes = opens + timedelta(minutes=5)
    window = PolymarketListedWindow(asset="BTC", horizon=5, slug="btc-updown-5m-1",
                                    opens=opens, closes=closes, accepting_orders=True,
                                    price_to_beat=None)
    board = PolymarketWindowBoard(checked_at=now, current=window, next_window=None,
                                  current_quote=None)
    channel = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_partial_messageable=lambda _id: channel)
    service = LivePolymarketBoardService(bot=bot, store=None, reader=None, market=None)
    tracker = LivePolymarketTracker(user_id="7", asset="BTC", horizon=5, channel_id="1",
                                    message_id="2", fingerprint="f")
    call = _call(window.slug, closes, "UP", now - timedelta(seconds=1))
    changes = []
    await service._alert_change(tracker, board, call, changes)   # baseline, no alert
    channel.send.assert_not_awaited()
    for n in range(MAX_CHANGE_ALERTS + 2):
        changes.append((now, "DOWN" if n % 2 == 0 else "UP", "UP" if n % 2 == 0 else "DOWN"))
        await service._alert_change(tracker, board, call, changes)
        await service._alert_change(tracker, board, call, changes)  # same flip: once only
    assert channel.send.await_count == MAX_CHANGE_ALERTS
    first = channel.send.await_args_list[0].kwargs
    assert "changed its BTC 5m Polymarket call: DOWN → UP" in first["content"]
    assert "Not a trading signal" in first["embed"].description
    assert "no more alerts" in channel.send.await_args_list[-1].kwargs["embed"].description
