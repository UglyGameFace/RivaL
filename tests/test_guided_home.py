"""Beginner RivaL menu contracts: actual navigation, actions and owner privacy."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.home import EXPLAIN, GuidedHome
from parlay_bot.perpetuals.live_quotes import BookQuote


def _client(tmp_path):
    return RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "beginner.sqlite"), discord_token=None,
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


def _interaction(owner=1234567, interaction_id=9000001):
    return SimpleNamespace(
        id=interaction_id, user=SimpleNamespace(id=owner),
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _labels(view):
    return [i.label for i in view.children if isinstance(i, discord.ui.Button)]


@pytest.mark.asyncio
async def test_beginner_home_begins_simple_and_keeps_advanced_features(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        assert bot.tree.get_command("rival") is not None
        assert "simple private home" in bot.tree.get_command("rival").description
        assert bot.tree.get_command("rival-picks") is not None
        assert bot.tree.get_command("rival-research") is not None
        assert bot.tree.get_command("rival-profit-proof") is not None
        assert bot.tree.get_command("rival-practice") is not None
        assert home.page == "home"
        assert {"Sports & Odds", "Predict Crypto", "Practice Money",
                "My Results", "Help Me Start", "Advanced",
                "Home", "Back", "Explain This"}.issubset(set(_labels(home)))
        assert len(home.children) <= 25
        assert max(item.row for item in home.children) <= 4
        embed = await home.render()
        assert "What would you like to do?" in embed.description
        assert "No real bets" in embed.description
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_every_page_has_home_back_explain_without_dead_buttons(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        for page in ("sports", "crypto", "practice", "results",
                     "help", "advanced", "slip", "forecast", "quote"):
            home.navigate("home")
            home.navigate(page)
            home._buttons()
            buttons = _labels(home)
            assert {"Home", "Back", "Explain This"}.issubset(set(buttons))
            assert len(home.children) <= 25
            assert all(0 <= item.row <= 4 for item in home.children)
            assert page in EXPLAIN
        i = _interaction()
        await home.act(i, "back")
        assert home.page == "home"
        await home.act(i, "help")
        assert home.page == "help"
        assert "Getting Started" in (await home.render()).title
        await home.act(i, "explain")
        i.response.send_message.assert_awaited_with(EXPLAIN["help"], ephemeral=True)
        await home.act(i, "home")
        assert home.page == "home"
        assert not home.back_stack
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_user_isolation_prevents_other_people_using_private_menu(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        stranger = _interaction(owner=887777)
        assert not await home.interaction_check(stranger)
        stranger.response.send_message.assert_awaited_once()
        assert home.page == "home"
        assert await home.interaction_check(_interaction())
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_sports_guided_onboarding_and_helpful_pass(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        i = _interaction()
        await home.act(i, "sports")
        assert "First Step" in (await home.render()).title
        await home.submit(i, "state", "Connecticut")
        assert home.page == "sports"
        assert "Saved" in home.note
        assert "Sports & Odds" in (await home.render()).title
        await home.act(i, "slip")
        assert "PASS" in home.note or home.page == "slip"
        await home.act(i, "home")
        assert home.page == "home"
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_crypto_watch_and_price_goal_are_existing_service_actions(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    bot.direction_targets.follow = AsyncMock(return_value=(True, "DM watch started"))
    try:
        i = _interaction()
        await home.act(i, "crypto")
        assert "Choose a coin" in next(
            item.placeholder for item in home.children
            if isinstance(item, discord.ui.Select)
        )
        home.coin = "ETH"
        home.horizon = 15
        await home.act(i, "follow")
        bot.direction_targets.follow.assert_awaited_with(
            i.user, asset="ETH", horizon_minutes=15,
        )
        assert "DM watch started" in home.note
        await home.submit(i, "target", "4200.00")
        call = bot.direction_targets.follow.await_args
        assert call.kwargs["target_price"] == "4200"
        assert call.kwargs["asset"] == "ETH"
        assert call.kwargs["horizon_minutes"] == 15
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_practice_uses_fake_balance_and_real_quote_gate(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        i = _interaction()
        await home.act(i, "practice")
        assert "My Practice Money" in (await home.render()).title
        assert bot.paper_ledger.summary(str(home.owner_id))["practice"]["funded"] == 10000
        await home.submit(i, "funds", "2500")
        assert bot.paper_ledger.summary(str(home.owner_id))["practice"]["funded"] == 2500
        entry_at = datetime.now(UTC) - timedelta(seconds=3)
        exit_at = datetime.now(UTC) - timedelta(seconds=1)

        async def quote(_asset):
            observed = entry_at if bot.paper_ledger.summary(
                str(home.owner_id),
            )["open"] == 0 else exit_at
            return BookQuote(
                "BTC", "binance_usdm", "BTCUSDT",
                100.0, 10.0, 100.1, 10.0, observed, observed,
            )

        bot.public_quotes.get = quote
        await home.act(i, "buy")
        assert bot.paper_ledger.summary(str(home.owner_id))["open"] == 1
        assert bot.paper_ledger.summary(str(home.owner_id))[
            "practice"
        ]["committed"] == 25
        await home.act(i, "close")
        assert bot.paper_ledger.summary(str(home.owner_id))["open"] == 0
        assert bot.paper_ledger.summary(str(home.owner_id))["closed"] == 1
        assert bot.paper_ledger.summary(str(home.owner_id))[
            "actual_profit_verified"
        ] is False
        await home.act(i, "results")
        assert "Real-Money Profit" in [
            field.name for field in (await home.render()).fields
        ]
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_live_quote_failure_is_not_masked_as_a_practice_success(tmp_path):
    from parlay_bot.perpetuals.live_quotes import QuoteUnavailable

    bot = _client(tmp_path)
    home = _home(bot)
    try:
        i = _interaction()
        await home.act(i, "practice")
        bot.public_quotes.get = AsyncMock(
            side_effect=QuoteUnavailable("Region blocked"),
        )
        await home.act(i, "buy")
        assert "Region blocked" in home.note
        assert bot.paper_ledger.summary(str(home.owner_id))["open"] == 0
        await home.act(i, "home")
        assert home.page == "home"
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_actual_rival_slash_callback_opens_guided_private_home(tmp_path):
    bot = _client(tmp_path)
    try:
        interaction = _interaction()
        await bot.tree.get_command("rival").callback(interaction)
        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True,
        )
        interaction.edit_original_response.assert_awaited_once()
        sent = interaction.edit_original_response.await_args.kwargs
        assert isinstance(sent["view"], GuidedHome)
        assert sent["view"].owner_id == interaction.user.id
        assert sent["embed"].title == "RivaL | Betting Genie"
        assert "No real bets" in sent["embed"].description
        assert "Home" in _labels(sent["view"])
        sent["view"].stop()
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_private_signal_button_explains_no_forecast_without_fake_prices(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        home.navigate("crypto")
        home._buttons()
        assert "Get Signal" in _labels(home)
        assert "See Forecast" not in _labels(home)
        assert "NO CURRENT SIGNAL" in (await home.render()).description
        i = _interaction()
        await home.act(i, "forecast")
        assert home.page == "forecast"
        sent = i.edit_original_response.await_args.kwargs["embed"]
        assert "NO LIVE SIGNAL" in sent.title
        assert "Market collection is disconnected" in sent.description
        assert "Home" in _labels(home)
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_rival_signal_slash_command_is_private_and_uses_same_signal_engine(tmp_path):
    from discord import app_commands

    bot = _client(tmp_path)
    try:
        command = bot.tree.get_command("rival-signal")
        assert command is not None
        i = _interaction()
        await command.callback(
            i,
            app_commands.Choice(name="Bitcoin (BTC)", value="BTC"),
            app_commands.Choice(name="5 minutes", value=5),
        )
        i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        i.edit_original_response.assert_awaited_once()
        assert "NO LIVE SIGNAL" in i.edit_original_response.await_args.kwargs["embed"].title
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
