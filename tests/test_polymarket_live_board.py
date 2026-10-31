"""Automatic live Polymarket DM keeps one private message current."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from parlay_bot.discord_app.polymarket_live_board import (
    LivePolymarketBoardService,
    LivePolymarketBoardStore,
    LivePolymarketTracker,
    live_polymarket_embed,
)
from parlay_bot.discord_app.polymarket_watch import (
    PolymarketListedWindow,
    PolymarketNotice,
    PolymarketWindowBoard,
)
from parlay_bot.discord_app.targeting import TargetSnapshot
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.mark.asyncio
async def test_slow_market_cannot_hold_other_boards_or_stop(tmp_path, monkeypatch):
    hot, store, service, _channel, user, edited = _setup(tmp_path)
    slow_started, release, fast_edited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    store.save(LivePolymarketTracker(str(user.id), "BTC", 5, "555", "901", "old"))
    store.save(LivePolymarketTracker("987654321", "ETH", 5, "556", "902", "old"))

    async def observe(asset, horizon):
        if asset == "BTC":
            slow_started.set()
            await release.wait()
        return discord.Embed(title=asset), "new"

    async def edit(**kw):
        if kw["embed"].title == "ETH":
            fast_edited.set()

    edited.side_effect = edit
    monkeypatch.setattr(service, "_observation", observe)
    cycle = asyncio.create_task(service.cycle())
    try:
        await asyncio.wait_for(slow_started.wait(), timeout=1)
        await asyncio.wait_for(fast_edited.wait(), timeout=1)
        # Stop does not wait for another market's slow read, and that read
        # cannot restore the card after it completes.
        assert await asyncio.wait_for(service.stop(user.id), timeout=1)
    finally:
        release.set()
    try:
        assert await cycle == 1
        assert all(call.kwargs["embed"].title != "BTC" for call in edited.await_args_list)
        assert store.get(str(user.id)) is None
        assert store.get("987654321").fingerprint == "new"
    finally:
        hot.close()


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
        opens=now, closes=now + timedelta(minutes=5),
        accepting_orders=True, price_to_beat=None,
    )
    next_window = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-1793407200",
        opens=now + timedelta(minutes=5),
        closes=now + timedelta(minutes=10),
        accepting_orders=False, price_to_beat=None,
    )
    book = PolymarketNotice(
        asset="BTC", horizon=5, issued=now,
        expires=current.closes, slug=current.slug,
        up_ask=0.61, down_ask=0.45, quote_at=now,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current,
        next_window=next_window, current_quote=book,
    )
    model = TargetSnapshot(
        asset="BTC", horizon_minutes=5, direction="UP",
        detail="Research only", venue="hyperliquid",
        probability=0.59, price=78000.0, issued_at=now,
        ends_at=current.closes, status="ok",
    )
    embed = live_polymarket_embed(board, model, asset="BTC", horizon=5)
    assert "Polymarket AI WAIT" in embed.title
    assert "No current contract-specific AI call" in embed.description
    full = str(embed.to_dict())
    assert "UP $0.61" in full and "DOWN $0.45" in full
    assert "hyperliquid" not in full and "59.0%" not in full
    assert next_window.slug in full
    assert "~15s" in embed.footer.text


def test_15m_model_leans_up_for_a_different_five_minute_candle_only():
    now = datetime(2026, 10, 30, 19, 15, tzinfo=UTC)
    current = PolymarketListedWindow(
        asset="BTC", horizon=15, slug="btc-updown-15m-1",
        opens=now, closes=now + timedelta(minutes=15),
        accepting_orders=True, price_to_beat=75000.0,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current, next_window=None,
        current_quote=PolymarketNotice(
            asset="BTC", horizon=15, issued=now,
            expires=current.closes, slug=current.slug,
            up_ask=0.82, down_ask=0.19, quote_at=now,
        ),
    )
    model = TargetSnapshot(
        asset="BTC", horizon_minutes=15, direction="UP",
        detail="not calibrated", venue="hyperliquid",
        probability=0.95, price=75000.0, issued_at=now,
        ends_at=current.closes, status="ok",
    )
    embed = live_polymarket_embed(board, model, asset="BTC", horizon=15)
    assert "Polymarket AI WAIT" in embed.title
    full = str(embed.to_dict())
    assert "No current contract-specific AI call" in full
    assert "hyperliquid" not in full and "95.0%" not in full


def test_stale_or_unavailable_model_never_becomes_directional_call():
    now = datetime(2026, 10, 30, 19, 15, tzinfo=UTC)
    current = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-1",
        opens=now, closes=now + timedelta(minutes=5),
        accepting_orders=True, price_to_beat=None,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current, next_window=None,
        current_quote=None,
    )
    for snapshot in (
        None,
        TargetSnapshot(
            asset="BTC", horizon_minutes=5, direction="DOWN",
            detail="expired candle", venue="okx",
            probability=0.99, price=75000.0,
            issued_at=now - timedelta(minutes=5),
            ends_at=now - timedelta(seconds=1), status="ok",
        ),
    ):
        embed = live_polymarket_embed(board, snapshot, asset="BTC", horizon=5)
        assert "No current contract-specific AI call" in embed.description
        assert "Polymarket AI WAIT" in embed.title
        assert "99.0%" not in str(embed.to_dict())


def test_live_polymarket_hourly_settlement_source_follows_quote_not_timeframe():
    now = datetime(2026, 10, 30, 12, 10, tzinfo=UTC)
    start = now.replace(minute=0)
    market = PolymarketListedWindow(
        asset="BTC", horizon=60, slug="btc-up-or-down-test",
        opens=start, closes=start+timedelta(hours=1),
        accepting_orders=True, price_to_beat=72000.,
    )
    def picture(source):
        notice = PolymarketNotice(
            asset="BTC", horizon=60, issued=start, expires=market.closes,
            slug=market.slug, up_ask=.55, down_ask=.48,
            quote_at=now, resolution_source=source,
        )
        board = PolymarketWindowBoard(
            checked_at=now, current=market, next_window=None,
            current_quote=notice,
        )
        result = live_polymarket_embed(board, None, asset="BTC", horizon=60)
        return next(f.value for f in result.fields if f.name == "Settlement source")

    assert "Chainlink" in picture(
        "https://data.chain.link/streams/btc-usd-twap-60s-streams"
    )
    assert "Binance" not in picture(
        "https://data.chain.link/streams/btc-usd-twap-60s-streams"
    )
    assert "Binance" in picture("https://www.binance.com/en/trade/BTC_USDT")
    assert "not verified" in picture(None)


@pytest.mark.asyncio
async def test_market_picker_and_live_board_do_not_spend_free_ai_quota(tmp_path):
    hot, _store, service, _channel, _user, _edited = _setup(tmp_path)
    now = datetime.now(UTC)
    start = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    current = PolymarketListedWindow(
        asset="BTC", horizon=5, slug=f"btc-updown-5m-{int(start.timestamp())}",
        opens=start, closes=now+timedelta(minutes=4),
        accepting_orders=True, price_to_beat=65000.,
    )
    notice = PolymarketNotice(
        asset="BTC", horizon=5, slug=current.slug, issued=start,
        expires=current.closes, up_ask=.6, down_ask=.4, quote_at=now,
        condition_id="0x"+"1"*64,
        up_token_id="1"*60, down_token_id="2"*60,
        resolution_source="https://data.chain.link",
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current, next_window=None,
        current_quote=notice,
    )
    calls = Mock(return_value=None)
    research = SimpleNamespace(
        ai=SimpleNamespace(available=True),
        observe=calls, peek=lambda slug: None,
        status=lambda slug: "No call requested.",
        refresh_note=lambda slug: None,
        wait=AsyncMock(),
    )
    service.reader = SimpleNamespace(window_board=AsyncMock(return_value=board))
    service.research = research
    try:
        await service._observation("BTC", 5)
        calls.assert_called_once_with(notice)
        # Explicit contract call is the ONLY API-capable path.
        assert await service.request_contract_ai("BTC", 5)
        assert calls.call_count == 2
        assert calls.call_args.kwargs == {"request_ai": True}
        # Further auto board/price refreshes must remain math-only.
        await service._observation("BTC", 5)
        assert calls.call_args.kwargs == {}
        research.ai.available = False
        assert not await service.request_contract_ai("BTC", 5)
        assert calls.call_count == 3
    finally:
        await service.aclose()
        hot.close()
