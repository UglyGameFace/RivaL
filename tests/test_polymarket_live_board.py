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
async def test_auto_tracking_persists_sends_a_fresh_card_and_stops(tmp_path, monkeypatch):
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
        assert channel.send.await_args.kwargs["view"] is service.dm_view
        assert service.dm_view.timeout is None
        assert {button.custom_id for button in service.dm_view.children} == {
            "rival:poly:dm:refresh", "rival:poly:dm:stop", "rival:poly:dm:pings",
            "rival:poly:dm:record", "rival:poly:dm:market",
        }
        tracker = store.get(str(user.id))
        assert tracker is not None
        assert tracker.message_id == "901"
        assert tracker.fingerprint == "fingerprint-1"
        assert await service.cycle() == 0
        assert edited.await_count == 0

        # Another tap sends a fresh card at the bottom of the DMs (the old
        # one is retired), so pressing Track always visibly does something.
        channel.send.return_value = SimpleNamespace(id=902)
        ok, message = await service.start(user, asset="XRP", horizon=15)
        assert ok and "XRP 15m" in message
        assert channel.send.await_count == 2
        assert store.get(str(user.id)).message_id == "902"
        assert "moved to a newer message" in edited.await_args.kwargs["embed"].description
        assert edited.await_args.kwargs["view"] is None
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
        assert channel.send.await_count == 2  # stop/cycles send nothing new
        assert not await restarted.stop(user.id)
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_deleted_panel_is_recreated_and_keeps_tracking(
    tmp_path, monkeypatch,
):
    hot, store, service, channel, user, _edited = _setup(tmp_path)
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
                    {"code": 10008, "message": "Unknown Message"},
                )

        channel.get_partial_message = lambda _id: MissingMessage()
        channel.send.return_value = SimpleNamespace(id=902)
        assert await service.cycle() == 1
        assert store.get(str(user.id)).message_id == "902"
        assert store.get(str(user.id)).fingerprint == "b"
        assert channel.send.await_count == 2
        assert channel.send.await_args.kwargs["view"] is service.dm_view
        assert await service.cycle() == 0
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_unknown_dm_destination_does_not_disable_tracking(tmp_path, monkeypatch):
    hot, store, service, channel, user, _edited = _setup(tmp_path)
    try:
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

        class MissingChannel:
            async def edit(self, **_kw):
                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"),
                    {"code": 10003, "message": "Unknown Channel"},
                )

        channel.get_partial_message = lambda _id: MissingChannel()
        assert await service.cycle() == 0
        assert store.get(str(user.id)).message_id == "901"
        channel.send.assert_awaited_once()  # never publish into unknown destination
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_dm_panel_actions_authorized_to_current_card(tmp_path, monkeypatch):
    hot, store, service, _channel, user, edited = _setup(tmp_path)
    try:
        monkeypatch.setattr(
            service, "_observation",
            AsyncMock(return_value=(discord.Embed(title="Current"), "a")),
        )
        ok, _ = await service.start(user, asset="BTC", horizon=5)
        assert ok
        response = SimpleNamespace(defer=AsyncMock())
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            user=user, message=SimpleNamespace(id=901), channel_id=555,
            response=response, followup=followup,
        )
        await service.handle_dm_action(interaction, "refresh")
        response.defer.assert_awaited_once()
        followup.send.assert_awaited_once()
        assert "refreshed" in followup.send.await_args.args[0]
        assert edited.await_args.kwargs["view"] is service.dm_view

        # Old panels and another channel cannot stop an active tracker.
        assert not await service.stop(user.id, expected_message_id="902",
                                      expected_channel_id="555")
        assert not await service.stop(user.id, expected_message_id="901",
                                      expected_channel_id="666")
        assert store.get(str(user.id)) is not None

        interaction.message.id = 902
        await service.handle_dm_action(interaction, "stop")
        assert "older panel" in followup.send.await_args.args[0]
        assert store.get(str(user.id)) is not None
        interaction.message.id = 901
        await service.handle_dm_action(interaction, "stop")
        assert store.get(str(user.id)) is None
        assert edited.await_args.kwargs["view"] is None
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
    assert "Forecast pending" in embed.title
    assert "No current contract-specific AI call" in embed.description
    full = str(embed.to_dict())
    assert "UP $0.61" in full and "DOWN $0.45" in full
    assert "hyperliquid" not in full and "59.0%" not in full
    assert next_window.slug in full
    assert "Next contract • forecast pending" in full
    assert "WAIT / not issued" in full
    assert "First model call frozen" in embed.footer.text


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
    assert "Forecast pending" in embed.title
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
        assert "Forecast pending" in embed.title
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


@pytest.mark.asyncio
async def test_live_boards_bound_quote_reads_but_do_not_hold_dm_edits(tmp_path, monkeypatch):
    """A full 12-contract tracker fanout does not hammer Gamma/CLOB."""
    from parlay_bot.discord_app.polymarket_watch import ASSETS, HORIZONS

    hot, store, service, _channel, _user, edited = _setup(tmp_path)
    ongoing = 0
    maximum = 0
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    for n, (asset, horizon) in enumerate(
        (pair for asset in ASSETS for pair in ((asset, h) for h in HORIZONS)),
        start=1001,
    ):
        store.save(LivePolymarketTracker(
            str(n), asset, horizon, "555", str(n+5000), "old",
        ))

    async def quote_observation(asset, horizon):
        nonlocal ongoing, maximum
        ongoing += 1
        maximum = max(maximum, ongoing)
        if ongoing == 4:
            started.set()
        try:
            await release.wait()
            observed.append((asset, horizon))
            return discord.Embed(title=f"{asset}/{horizon}"), "new"
        finally:
            ongoing -= 1

    monkeypatch.setattr(service, "_observation", quote_observation)
    cycle = asyncio.create_task(service.cycle())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert ongoing == 4
        assert maximum == 4
    finally:
        release.set()
    try:
        assert await asyncio.wait_for(cycle, timeout=3) == 12
        assert maximum == 4
        assert len(observed) == len(ASSETS) * len(HORIZONS)
        assert edited.await_count == 12
        assert all(tracker.fingerprint == "new" for tracker in store.active())
    finally:
        hot.close()


def _frozen_call(current, *, direction="DOWN", issued=None, p=0.417):
    from parlay_bot.perpetuals.polymarket_research import ContractCall
    return ContractCall(
        slug=current.slug, asset=current.asset, horizon=current.horizon,
        issued_at=issued or current.opens, expires=current.closes,
        direction=direction, model="book", thesis="frozen", risk="research",
        evidence_sha256="e" * 64, kind="model", probability_up=p,
    )


def _open_board(now, *, quote=True):
    current = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-77",
        opens=now - timedelta(seconds=5), closes=now + timedelta(seconds=295),
        accepting_orders=True, price_to_beat=None,
    )
    book = PolymarketNotice(
        asset="BTC", horizon=5, issued=current.opens, expires=current.closes,
        slug=current.slug, up_ask=0.46, down_ask=0.55, quote_at=now,
    ) if quote else None
    return current, PolymarketWindowBoard(
        checked_at=now, current=current, next_window=None, current_quote=book,
        quote_unavailable_reason=None if quote else "no_liquidity",
    )


@pytest.mark.asyncio
async def test_card_keeps_frozen_call_when_quote_drops_out(tmp_path):
    """Production 2026-11-06: an empty ask side (no_liquidity) near the close
    removed the quote and the card fell back to WAIT despite a stored call."""
    hot, _store, service, *_ = _setup(tmp_path)
    try:
        current, board = _open_board(datetime.now(UTC), quote=False)
        call = _frozen_call(current)
        service.reader = SimpleNamespace(window_board=AsyncMock(return_value=board))
        service.research = SimpleNamespace(
            peek=Mock(return_value=call), observe=Mock(),
            refresh_note=Mock(return_value=None), status=Mock(return_value=""),
        )
        embed, _ = await service._observation("BTC", 5)
        assert "Research DOWN" in embed.title and "unqualified" in embed.title
        assert any("DOWN" in f.value for f in embed.fields if "model research" in f.name)
        service.research.observe.assert_not_called()
        assert "No verified sell-side liquidity" in next(
            f.value for f in embed.fields if "share asks" in f.name)
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_one_ping_per_contract_only_for_fresh_up_down_calls(tmp_path, monkeypatch):
    hot, store, service, channel, user, _edited = _setup(tmp_path)
    try:
        store.save(LivePolymarketTracker(str(user.id), "BTC", 5, "555", "901", "old"))
        now = datetime.now(UTC)
        current, board = _open_board(now)
        from dataclasses import replace

        from parlay_bot.perpetuals.model_league import settlement_schema
        quote = replace(board.current_quote, down_cost=.5275, up_cost=.6275,
                        resolution_source='https://data.chain.link/streams/btc-usd-twap-60s-streams',
                        rules='Chainlink 60-second TWAP', condition_id='0x'+'a'*64)
        board = replace(board, current_quote=quote)
        call = replace(_frozen_call(current, issued=now - timedelta(seconds=3)),
                       settlement_schema=settlement_schema(quote), condition_id=quote.condition_id)
        service.research = SimpleNamespace(learner=SimpleNamespace(summary=lambda **_: {
            "settled_forward_contracts": 2284, "forward_brier": 0.2134,
            "market_baseline_brier": 0.2117}))

        async def observe(asset, horizon):
            service._live_cache[(asset, horizon)] = (datetime.now(UTC), board, current_call)
            return discord.Embed(title="card"), "fp"

        monkeypatch.setattr(service, "_observation", observe)
        current_call = call
        # No proven champion for this market: the main model never pings.
        service._league = (datetime.now(UTC), {"markets": {("BTC", 5): {
            "models": [], "champion": None}}})
        await service.cycle()
        assert not [c for c in channel.send.await_args_list if "content" in c.kwargs]
        # Once the main model is the market's proven best, it pings.
        service._league = (datetime.now(UTC), {"markets": {("BTC", 5): {
            "scopes": {settlement_schema(quote): {
                "models": [], "champion": call.model}}}}})
        await service.cycle()
        await service.cycle()
        pings = [c for c in channel.send.await_args_list if "content" in c.kwargs]
        assert len(pings) == 1  # a second cycle never re-pings the same contract
        text = pings[0].kwargs["content"]
        assert "call issued: DOWN" in text and "41.7% UP" in text
        assert "passed the sequential contest" in text and "Paper evidence" in text
        assert "will not flip" in text
        assert "Price check: DOWN costs" in text  # 58% DOWN vs 55¢ + fee

        # WAIT, an old call (e.g. after a restart) and the final minute never ping.
        for current_call in (
            _frozen_call(current, direction="WAIT", issued=now, p=0.48),
            _frozen_call(current, issued=now - timedelta(seconds=90)),
        ):
            service._pinged.clear()
            await service.cycle()
        late = PolymarketListedWindow(
            asset="BTC", horizon=5, slug="btc-updown-5m-78",
            opens=now - timedelta(seconds=250), closes=now + timedelta(seconds=50),
            accepting_orders=True, price_to_beat=None,
        )
        board = PolymarketWindowBoard(checked_at=now, current=late, next_window=None,
                                      current_quote=None)
        current_call = _frozen_call(late, issued=now - timedelta(seconds=2))
        await service.cycle()
        assert len([c for c in channel.send.await_args_list if "content" in c.kwargs]) == 1
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_dm_card_switches_market_in_place_and_toggles_pings(tmp_path, monkeypatch):
    hot, store, service, channel, user, edited = _setup(tmp_path)
    try:
        seen = []

        async def observe(asset, horizon):
            seen.append((asset, horizon))
            return discord.Embed(title=f"{asset} {horizon}"), f"{asset}{horizon}"

        monkeypatch.setattr(service, "_observation", observe)
        assert (await service.start(user, asset="BTC", horizon=5))[0]
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            user=user, message=SimpleNamespace(id=901), channel_id=555,
            response=SimpleNamespace(defer=AsyncMock()), followup=followup,
        )
        await service.handle_dm_action(interaction, "market:ETH:15")
        assert "now tracks ETH 15m" in followup.send.await_args.args[0]
        tracker = store.get(str(user.id))
        assert (tracker.asset, tracker.horizon, tracker.message_id) == ("ETH", 15, "901")
        assert edited.await_args.kwargs["embed"].title == "ETH 15"
        assert channel.send.await_count == 1  # same card, no new message

        assert store.pings_on(str(user.id))
        await service.handle_dm_action(interaction, "pings")
        assert "Pings OFF" in followup.send.await_args.args[0]
        assert not store.pings_on(str(user.id))
        await service.handle_dm_action(interaction, "pings")
        assert "Pings ON" in followup.send.await_args.args[0]

        # An old card cannot retarget or toggle the active tracker.
        interaction.message.id = 777
        await service.handle_dm_action(interaction, "market:SOL:60")
        assert "older or inactive panel" in followup.send.await_args.args[0]
        assert store.get(str(user.id)).asset == "ETH"
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_dm_tap_latency_is_logged_and_an_expired_tap_is_recorded(tmp_path, caplog):
    import logging

    hot, _store, service, *_ = _setup(tmp_path)

    class Expired(discord.HTTPException):
        def __init__(self):
            pass

    try:
        late = SimpleNamespace(
            user=SimpleNamespace(id=1), message=SimpleNamespace(id=1), channel_id=1,
            created_at=datetime.now(UTC) - timedelta(seconds=4),
            response=SimpleNamespace(defer=AsyncMock(side_effect=Expired())),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with caplog.at_level(logging.INFO, logger="parlay_bot.discord_app.polymarket_live_board"):
            await service.handle_dm_action(late, "refresh")
        late.followup.send.assert_not_awaited()
        warning = next(r for r in caplog.records if "not acknowledged" in r.getMessage())
        assert warning.levelno == logging.WARNING and "receipt_ms=4" in warning.getMessage()
    finally:
        hot.close()


def test_price_check_line():
    from parlay_bot.discord_app.polymarket_live_board import value_line
    from parlay_bot.perpetuals.polymarket_research import ContractCall

    def call(direction, p):
        return ContractCall(slug="s", asset="BTC", horizon=5, issued_at=datetime.now(UTC),
                            expires=datetime.now(UTC) + timedelta(minutes=5), direction=direction,
                            model="m", thesis="t", risk="r", evidence_sha256="e",
                            kind="model", probability_up=p)
    expensive = value_line(call("UP", 0.559), SimpleNamespace(up_ask=0.66, down_ask=0.35, up_cost=.6757, down_cost=.3659))
    assert "costs 68¢" in expensive and "skip at this price" in expensive
    cheap = value_line(call("DOWN", 0.436), SimpleNamespace(up_ask=0.52, down_ask=0.49, up_cost=.5375, down_cost=.5075))
    assert "DOWN 56%" in cheap and "cheaper than the model's own chance" in cheap
    assert value_line(call("WAIT", 0.5), SimpleNamespace(up_ask=0.5, down_ask=0.5)) == ""


def test_price_check_never_invents_missing_costs():
    from parlay_bot.discord_app.polymarket_live_board import value_line
    from parlay_bot.perpetuals.polymarket_research import ContractCall

    call = ContractCall(slug="s", asset="BTC", horizon=5, issued_at=datetime.now(UTC),
                        expires=datetime.now(UTC)+timedelta(minutes=5), direction="UP", model="m",
                        thesis="t", risk="r", evidence_sha256="e", probability_up=.8)
    assert "unavailable" in value_line(call, SimpleNamespace(up_ask=.5, down_ask=.5))


@pytest.mark.asyncio
async def test_qualified_formula_can_serve_and_ping_from_its_exact_frozen_round(tmp_path):
    from parlay_bot.perpetuals.model_league import FORMULA, Contest, settlement_schema
    from tests.test_model_league import SHA, notice

    hot, store, service, channel, user, _ = _setup(tmp_path)
    try:
        now = datetime.now(UTC)
        quote = notice(start=now-timedelta(minutes=1), horizon=5)
        contest = Contest(hot)
        contest.initialize()
        assert contest.capture(quote, {FORMULA:(.9,SHA)}, now=now)
        current = PolymarketListedWindow('BTC',5,quote.slug,quote.issued,quote.expires,True,None)
        board = PolymarketWindowBoard(now,current,None,quote)
        service.research = SimpleNamespace(learner=SimpleNamespace(store=hot,summary=lambda **_: {}))
        service._league = (now, {'markets': {('BTC',5): {'scopes': {
            settlement_schema(quote): {'champion':FORMULA,'models':[]}}}}})
        call = await service._qualified_call(board, None, 'BTC', 5)
        assert call.model == FORMULA and call.direction == 'UP'
        assert call.probability_up == .9 and call.condition_id == quote.condition_id
        service._live_cache['BTC',5] = now,board,call
        tracker = LivePolymarketTracker(str(user.id),'BTC',5,'555','901','old')
        store.save(tracker)
        await service._ping(tracker,board,call)
        await service._ping(tracker,board,call)
        pings = [x for x in channel.send.await_args_list if 'content' in x.kwargs]
        assert len(pings) == 1 and 'call issued: UP' in pings[0].kwargs['content']
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_unqualified_checkpoint_forecast_is_visible_and_never_pings(tmp_path):
    from parlay_bot.perpetuals.model_league import BOOK, FORMULA, Contest, settlement_schema
    from tests.test_model_league import SHA, notice

    hot, store, service, channel, user, _ = _setup(tmp_path)
    try:
        now = datetime.now(UTC)
        quote = notice(start=now-timedelta(minutes=1), horizon=5)
        contest = Contest(hot)
        assert contest.capture(quote, {FORMULA: (.52,SHA), BOOK: (.1,SHA)}, now=now)
        current = PolymarketListedWindow('BTC',5,quote.slug,quote.issued,quote.expires,True,None)
        board = PolymarketWindowBoard(now,current,None,quote)
        service.research = SimpleNamespace(learner=SimpleNamespace(store=hot))
        service._league = (now, {'markets': {('BTC',5): {'scopes': {
            settlement_schema(quote): {'champion':None,'models':[]}}}}})
        call = await service._qualified_call(board, None, 'BTC', 5)
        assert call.model == FORMULA and call.direction == 'UP'  # forecast, despite no priced edge
        assert call.qualified_at_issue is False
        card = live_polymarket_embed(board,call,asset='BTC',horizon=5)
        assert 'Research UP' in card.title and 'unqualified' in card.title
        assert '52.0% UP' in str(card.to_dict())
        tracker = LivePolymarketTracker(str(user.id),'BTC',5,'555','901','old')
        store.save(tracker)
        await service._ping(tracker,board,call)
        assert not channel.send.await_args_list
        # A different model qualifying cannot flip this contract's UP display.
        service._league[1]['markets']['BTC',5]['scopes'][settlement_schema(quote)]['champion'] = BOOK
        again = await service._qualified_call(board,call,'BTC',5)
        assert again.direction == 'UP' and again.model == FORMULA
        service._league[1]['markets']['BTC',5]['scopes'][settlement_schema(quote)]['champion'] = FORMULA
        promoted = await service._qualified_call(board,again,'BTC',5)
        assert promoted.direction == 'UP' and promoted.qualified_at_issue is False
        card = live_polymarket_embed(board,promoted,asset='BTC',horizon=5,
                                     league={'champion':FORMULA,'models':[]})
        assert 'Research UP' in card.title and 'unqualified' in card.title
        await service._ping(tracker,board,again)
        assert not channel.send.await_args_list
    finally:
        hot.close()
