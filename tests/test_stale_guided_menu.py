"""Stale guided menus must recover instead of silently dying.

A live GuidedHome view disappears when its 15-minute timeout lapses or the
bot restarts, and discord.py then discards taps on it. The persistent
fallback views must cover every guided custom_id, reopen a fresh menu, and
never execute a stateful action from stale state.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.home import (
    _STALE_NOTE,
    GuidedHome,
    build_stale_guided_menu_views,
    install_view_dispatch_logging,
)
from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def _client(tmp_path):
    return RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "stale.sqlite"), discord_token="test-token",
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


def _custom_ids(view):
    return {item.custom_id for item in view.children}


def _guided_custom_ids(bot):
    """Every custom_id a live GuidedHome can show, across all its pages."""
    home = _home(bot)
    try:
        found: set[str] = set()
        for page in ("home", "discover", "sports", "slip", "crypto", "forecast", "quote",
                     "practice", "results", "help", "advanced", "feeds", "trust",
                     "hunt", "live"):
            home.navigate("home")
            home.navigate(page)
            home._buttons()
            found |= _custom_ids(home)
        return found
    finally:
        home.stop()


def _item_by_custom_id(view, custom_id):
    for item in view.children:
        if item.custom_id == custom_id:
            return item
    raise AssertionError(f"{custom_id} not in view")


@pytest.mark.asyncio
async def test_stale_views_are_persistent_and_cover_every_guided_custom_id(tmp_path):
    bot = _client(tmp_path)
    try:
        views = build_stale_guided_menu_views(lambda owner: _home(bot, owner))
        assert len(views) == 3
        covered: set[str] = set()
        for view in views:
            assert view.timeout is None
            assert view.is_persistent()
            assert len(view.children) <= 25
            ids = _custom_ids(view)
            assert None not in ids
            assert not (covered & ids), "duplicate custom_id across fallback views"
            covered |= ids
        assert _guided_custom_ids(bot) <= covered
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_stale_navigation_tap_reopens_fresh_menu(tmp_path):
    bot = _client(tmp_path)
    try:
        views = build_stale_guided_menu_views(lambda owner: _home(bot, owner))
        interaction = _interaction()
        item = _item_by_custom_id(views[0], "rival:guided:crypto")
        await item.callback(interaction)
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        _, kwargs = interaction.edit_original_response.await_args
        fresh = kwargs["view"]
        assert isinstance(fresh, GuidedHome)
        assert fresh.page == "crypto"
        assert fresh.note == _STALE_NOTE
        assert "What Happened" in [f.name for f in kwargs["embed"].fields]
        fresh.stop()
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_stale_stateful_tap_runs_nothing_and_reopens_home(tmp_path):
    bot = _client(tmp_path)
    try:
        views = build_stale_guided_menu_views(lambda owner: _home(bot, owner))
        interaction = _interaction()
        item = _item_by_custom_id(views[1], "rival:guided:buy")
        await item.callback(interaction)
        _, kwargs = interaction.edit_original_response.await_args
        fresh = kwargs["view"]
        assert isinstance(fresh, GuidedHome)
        assert fresh.page == "home"
        assert fresh.note == _STALE_NOTE
        # No trade, alert, or slip action ran: the fresh menu is untouched.
        assert fresh.slip is None
        interaction.followup.send.assert_not_awaited()
        fresh.stop()
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_stale_coin_select_recovers_with_chosen_coin(tmp_path):
    bot = _client(tmp_path)
    try:
        views = build_stale_guided_menu_views(lambda owner: _home(bot, owner))
        interaction = _interaction()
        coin_view = views[1]
        await coin_view.recover(interaction, coin="ETH")
        _, kwargs = interaction.edit_original_response.await_args
        fresh = kwargs["view"]
        assert fresh.coin == "ETH"
        assert fresh.page == "crypto"
        fresh.stop()
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


def _seed_watch(market, now):
    market.initialize()
    opened = now - timedelta(minutes=6)
    closed = now - timedelta(minutes=1)
    candle = MarketCandle(
        "hyperliquid", "BTC", "USDC", 5, opened, closed,
        closed + timedelta(milliseconds=1),
        80999, 81005, 80991, 81001, 20.0, 42,
    )
    market.ingest(MarketBatch((), (candle,), 5, closed + timedelta(milliseconds=1)))
    market.record_feed_health(
        venue="hyperliquid", asset="BTC", status="ok", attempted_at=now,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    ForwardAccuracyMonitor(market).initialize()
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "hyperliquid", "BTC", "USDC", "BTC", 5,
                "prospective-logistic-shadow-v1",
                opened.isoformat(), 81001,
                (now - timedelta(seconds=30)).isoformat(),
                (now + timedelta(seconds=120)).isoformat(),
                .61, .5, 120,
            ),
        )


@pytest.mark.asyncio
async def test_signal_evidence_runs_concurrently_with_book_fetch(tmp_path):
    """Forward proof must not wait behind the quote fetch (or vice versa).

    The book fetch sleeps while the shadow report records its start; under
    the old sequential code the shadow report could only start after the
    book finished, so this ordering assertion fails there and passes here.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "concurrent.sqlite")
    market = PerpetualMarketStore(store)
    marks: dict[str, int] = {}
    hyper = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=81000, ask=81001,
        bid_quantity=10, ask_quantity=10, exchange_at=now, received_at=now,
    )

    class SlowQuotes:
        async def get_reference(self, asset):
            marks["book_start"] = time.monotonic_ns()
            import asyncio as _asyncio
            await _asyncio.sleep(0.3)
            marks["book_end"] = time.monotonic_ns()
            return hyper

        async def get(self, asset):  # pragma: no cover - short-circuit path
            raise AssertionError("Binance must not be queried after Hyperliquid")

    class SlowShadow:
        def report(self, *, asset, horizon_minutes, now):
            marks["shadow_start"] = time.monotonic_ns()
            time.sleep(0.05)
            return {
                "status": "SHADOW", "closed": 3, "eligible_windows": 4,
                "days": 2,
                "costs": {"severe": {"mean_net_bps": 12.5}},
            }

    try:
        _seed_watch(market, now)
        from tests.test_rival_signal_decisions import seed_settled
        seed_settled(market, now, venue="hyperliquid")
        embed = await signal_embed(
            market, SlowQuotes(), asset="BTC", horizon_minutes=5, now=now,
            stream_shadow=SlowShadow(),
        )
        assert marks["shadow_start"] < marks["book_end"]
        assert "UP WATCH" in embed.title
        assert "Source-matched after-cost proof" in [f.name for f in embed.fields]
    finally:
        store.close()


def test_detached_message_item_uses_persistent_guided_recovery():
    """Reproduce Discloud's 'DISCARDED detached item rival:guided:hunt'."""
    key = (2, "rival:guided:hunt")
    stale = SimpleNamespace(view=None)
    persistent = SimpleNamespace(view=object())
    seen = []
    views = {987654: {key: stale}, None: {key: persistent}}

    def original(component_type, custom_id, interaction):
        matched = (
            views.get(interaction.message.id, {}).get((component_type, custom_id))
            or views.get(None, {}).get((component_type, custom_id))
        )
        seen.append(matched)
        return matched

    store = SimpleNamespace(_views=views, dispatch_view=original)
    client = SimpleNamespace(_connection=SimpleNamespace(_view_store=store))
    install_view_dispatch_logging(client)
    interaction = SimpleNamespace(message=SimpleNamespace(id=987654))

    assert store.dispatch_view(*key, interaction) is persistent
    assert seen == [persistent]
    assert key not in views[987654]
    # Retrying registration must not wrap the dispatcher twice.
    first = store.dispatch_view
    install_view_dispatch_logging(client)
    assert store.dispatch_view is first
    assert store.dispatch_view(*key, interaction) is persistent


def test_detached_non_guided_item_is_not_mutated():
    key = (2, "rival:other:some-button")
    stale = SimpleNamespace(view=None)
    persistent = SimpleNamespace(view=object())
    views = {123: {key: stale}, None: {key: persistent}}
    store = SimpleNamespace(
        _views=views, dispatch_view=lambda *args: views[123][key],
    )
    install_view_dispatch_logging(
        SimpleNamespace(_connection=SimpleNamespace(_view_store=store)),
    )
    result = store.dispatch_view(*key, SimpleNamespace(
        message=SimpleNamespace(id=123),
    ))
    assert result is stale
    assert views[123][key] is stale


@pytest.mark.asyncio
async def test_stale_live_mode_recovers_navigation_but_not_toggle(tmp_path):
    bot = _client(tmp_path)
    try:
        views = build_stale_guided_menu_views(lambda owner: _home(bot, owner))
        live = _interaction()
        await _item_by_custom_id(views[2], "rival:guided:live").callback(live)
        fresh = live.edit_original_response.await_args.kwargs["view"]
        assert fresh.page == "live"
        assert fresh.note == _STALE_NOTE
        fresh.stop()
        toggle = _interaction()
        await _item_by_custom_id(
            views[2], "rival:guided:live-on",
        ).callback(toggle)
        fresh = toggle.edit_original_response.await_args.kwargs["view"]
        assert fresh.page == "home"
        assert fresh.note == _STALE_NOTE
        fresh.stop()
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
