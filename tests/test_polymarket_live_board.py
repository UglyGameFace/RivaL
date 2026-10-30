"""Automatic live Polymarket DM keeps one private message current."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.discord_app.polymarket_live_board import (
    LivePolymarketBoardService,
    LivePolymarketBoardStore,
    live_polymarket_embed,
)
from parlay_bot.discord_app.polymarket_watch import (
    PolymarketListedWindow,
    PolymarketNotice,
    PolymarketWindowBoard,
)
from parlay_bot.discord_app.targeting import TargetSnapshot
from parlay_bot.storage.hot import SQLiteHotStore


def _setup(tmp_path):
    hot = SQLiteHotStore(tmp_path / "live_board.sqlite")
    hot.initialize()
    store = LivePolymarketBoardStore(hot)
    store.initialize()
    edited = AsyncMock()
    partial = SimpleNamespace(edit=edited)
    channel = SimpleNamespace(
        id=555,
        send=AsyncMock(return_value=SimpleNamespace(id=901)),
        get_partial_message=lambda _id: partial,
    )
    bot = SimpleNamespace(
        get_partial_messageable=lambda _id: channel,
    )
    user = SimpleNamespace(
        id=123456789, dm_channel=channel, create_dm=AsyncMock(),
    )
    service = LivePolymarketBoardService(
        bot=bot, store=store,
        reader=SimpleNamespace(), market=None,
    )
    return hot, store, service, channel, user, edited


@pytest.mark.asyncio
async def test_auto_tracking_persists_edits_one_dm_and_stops(tmp_path, monkeypatch):
    hot, store, service, channel, user, edited = _setup(tmp_path)
    one = discord.Embed(title="Current 5m")
    two = discord.Embed(title="Next 5m")
    monkeypatch.setattr(
        service, "_observation", AsyncMock(return_value=(one, "fingerprint-1")),
    )
    try:
        ok, message = await service.start(user, asset="BTC", horizon=5)
        assert ok and "Auto-tracking BTC" in message
        channel.send.assert_awaited_once()
        tracker = store.get(str(user.id))
        assert tracker is not None
        assert tracker.message_id == "901"
        assert tracker.fingerprint == "fingerprint-1"
        assert await service.cycle() == 0
        assert edited.await_count == 0

        # Multiple taps change the same DM rather than spamming users.
        ok, message = await service.start(user, asset="XRP", horizon=15)
        assert ok and "XRP 15m" in message
        assert channel.send.await_count == 1
        assert edited.await_count == 1
        edited.reset_mock()
        assert store.get(str(user.id)).asset == "XRP"

        monkeypatch.setattr(
            service, "_observation",
            AsyncMock(return_value=(two, "fingerprint-2")),
        )
        # Recreated service recovers the same durable message ID.
        restarted = LivePolymarketBoardService(
            bot=service.bot, store=LivePolymarketBoardStore(hot),
            reader=SimpleNamespace(), market=None,
        )
        monkeypatch.setattr(
            restarted, "_observation", service._observation,
        )
        assert await restarted.cycle() == 1
        edited.assert_awaited_once()
        assert edited.await_args.kwargs["embed"].title == "Next 5m"
        assert store.get(str(user.id)).fingerprint == "fingerprint-2"
        assert await restarted.cycle() == 0
        assert edited.await_count == 1

        assert await restarted.stop(user.id) is True
        assert store.get(str(user.id)) is None
        assert await restarted.cycle() == 0
        assert channel.send.await_count == 1
        assert not await restarted.stop(user.id)
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_auto_tracker_disallows_unsupported_window_and_stops_deleted_card(
    tmp_path, monkeypatch,
):
    hot, store, service, _channel, user, _edited = _setup(tmp_path)
    try:
        ok, _message = await service.start(user, asset="XRP", horizon=60)
        assert not ok and store.active() == []
        monkeypatch.setattr(
            service, "_observation",
            AsyncMock(return_value=(discord.Embed(title="Current"), "a")),
        )
        ok, _ = await service.start(user, asset="BTC", horizon=5)
        assert ok
        monkeypatch.setattr(
            service, "_observation",
            AsyncMock(return_value=(discord.Embed(title="Next"), "b")),
        )

        class MissingMessage:
            async def edit(self, **_kw):
                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"),
                    "Unknown Message",
                )

        service.bot.get_partial_messageable = lambda _id: SimpleNamespace(
            get_partial_message=lambda _id: MissingMessage(),
        )
        assert await service.cycle() == 0
        assert store.active() == []
    finally:
        hot.close()


def test_live_market_and_model_predictions_are_clearly_separate():
    now = datetime(2026, 10, 30, 19, 15, tzinfo=UTC)
    current = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-1793406900",
        opens=now, closes=now+timedelta(minutes=5),
        accepting_orders=True, price_to_beat=None,
    )
    following = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-1793407200",
        opens=now+timedelta(minutes=5),
        closes=now+timedelta(minutes=10),
        accepting_orders=False, price_to_beat=None,
    )
    book = PolymarketNotice(
        asset="BTC", horizon=5, issued=now,
        expires=now+timedelta(minutes=5),
        slug=current.slug,
        up_ask=0.61, down_ask=0.45,
        quote_at=now,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current, next_window=following,
        current_quote=book,
    )
    model = TargetSnapshot(
        asset="BTC", horizon_minutes=5, direction="UP",
        detail="Research only", venue="hyperliquid",
        probability=0.59, price=78000.0, issued_at=now,
        ends_at=now+timedelta(minutes=5), status="ok",
    )
    embed = live_polymarket_embed(board, model, asset="BTC", horizon=5)
    full_text = "\n".join(str(field.value) for field in embed.fields)
    assert "Up ask 61.0%" in full_text
    assert "Down ask 45.0%" in full_text
    assert "Price to beat: Not verified" in full_text
    assert "Next price to beat unknown" in full_text
    assert "Same 5m closing boundary verified" in full_text
    assert "lean: UP" in full_text
    assert "source hyperliquid" in full_text
    assert "WAIT • no verified Polymarket-specific edge" in full_text
    assert "not calibrated" in full_text
    assert "every 15s" in embed.footer.text


def test_15m_model_cannot_be_presented_as_15m_polymarket_prediction():
    now = datetime(2026, 10, 30, 19, 15, tzinfo=UTC)
    current = PolymarketListedWindow(
        asset="BTC", horizon=15, slug="btc-updown-15m-1",
        opens=now, closes=now + timedelta(minutes=15),
        accepting_orders=True, price_to_beat=75000.0,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current,
        next_window=None,
        current_quote=PolymarketNotice(
            asset="BTC", horizon=15, issued=now,
            expires=now+timedelta(minutes=15),
            slug=current.slug, up_ask=0.82,
            down_ask=0.19, quote_at=now,
        ),
    )
    model = TargetSnapshot(
        asset="BTC", horizon_minutes=15, direction="UP",
        detail="not validated", venue="hyperliquid",
        probability=0.95, price=75000.0, issued_at=now,
        ends_at=current.closes, status="ok",
    )
    embed = live_polymarket_embed(board, model, asset="BTC", horizon=15)
    decision = embed.fields[-2].value
    model_line = embed.fields[-1].value
    assert "WAIT" in decision
    assert "share purchase prices" in decision
    assert "WAIT" in model_line
    assert "5-minute candle" in model_line
    assert "95.0%" not in model_line
    assert "UP" not in model_line


def test_mismatched_or_expired_forecast_does_not_justify_direction():
    now = datetime(2026, 10, 30, 19, 15, tzinfo=UTC)
    current = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-1",
        opens=now, closes=now+timedelta(minutes=5),
        accepting_orders=True, price_to_beat=None,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current, next_window=None,
        current_quote=PolymarketNotice(
            asset="BTC", horizon=5, issued=now,
            expires=now+timedelta(minutes=5),
            slug=current.slug, up_ask=0.60,
            down_ask=0.42, quote_at=now,
        ),
    )
    wrong_window = TargetSnapshot(
        asset="BTC", horizon_minutes=5, direction="DOWN",
        detail="old unrelated candle", venue="okx",
        probability=0.99, price=75000.0, issued_at=now,
        ends_at=now+timedelta(minutes=10), status="ok",
    )
    embed = live_polymarket_embed(
        board, wrong_window, asset="BTC", horizon=5,
    )
    evidence = embed.fields[-1].value
    assert "WAIT" in evidence
    assert "99.0%" not in evidence
    assert "DOWN" not in evidence
    assert "no verified Polymarket-specific edge" in embed.fields[-2].value
