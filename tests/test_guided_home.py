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
        tools = bot.tree.get_command("rival-tools")
        assert tools is not None
        assert tools.get_command("picks") is not None
        assert tools.get_command("research") is not None
        assert tools.get_command("profit-proof") is not None
        assert tools.get_command("practice") is not None
        assert home.page == "home"
        assert set(_labels(home)) == {
            "Find Opportunities", "My Alerts", "Sports & Odds",
            "My Results", "Home", "Back", "Explain This",
        }
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
        for page in ("discover", "sports", "crypto", "practice", "results",
                     "help", "advanced", "slip", "forecast", "quote",
                     "feeds", "trust"):
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
        assert "Sports & Odds" in (await home.render()).title
        assert "Not set" in (await home.render()).fields[0].value
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
        display = i.edit_original_response.await_args.kwargs["view"]
        assert "Choose a coin" in next(
            item.placeholder for item in display.children
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
        assert "Collector unavailable — signals paused" in (await home.render()).description
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


@pytest.mark.asyncio
async def test_guided_results_uses_strict_source_matched_cost_proof(tmp_path):
    from unittest.mock import Mock

    bot = _client(tmp_path)
    home = _home(bot)
    audit = Mock()
    audit.report.return_value = {
        "status": "NOT_PROVEN", "closed": 2, "eligible_windows": 9,
        "days": 1, "costs": {"severe": {"mean_net_bps": -14.25}},
    }
    home.stream_shadow = audit
    try:
        home.navigate("results")
        i = _interaction()
        await home.act(i, "proof")
        audit.report.assert_called_once_with(
            asset="BTC", horizon_minutes=5,
        )
        embed = i.edit_original_response.await_args.kwargs["embed"]
        assert "2/9" in str(embed.to_dict())
        assert "-14.2 bps" in str(embed.to_dict())
        assert "NOT_PROVEN" in str(embed.to_dict())
        assert "Real-money profit NOT PROVEN" in str(embed.to_dict())
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_old_and_new_profit_commands_use_same_strict_view(tmp_path):
    from unittest.mock import Mock

    from discord import app_commands

    bot = _client(tmp_path)
    shadow = Mock()
    shadow.report.return_value = {
        "status": "NOT_PROVEN", "asset": "BTC", "horizon_minutes": 5,
        "eligible_windows": 7, "entries": 3, "closed": 2,
        "missed_exits": 1, "coverage": 2 / 7, "days": 1,
        "costs": {
            "conservative": {
                "assumed_extra_cost_bps": 14,
                "mean_net_bps": -3, "daily_lower_bps": None,
            },
            "severe": {
                "assumed_extra_cost_bps": 80,
                "mean_net_bps": -69, "daily_lower_bps": None,
            },
        },
    }
    bot.stream_shadow = shadow
    try:
        coin = app_commands.Choice(name="BTC", value="BTC")
        horizon = app_commands.Choice(name="5 minutes", value=5)
        views = []
        for name in ("stream-proof", "profit-proof"):
            i = _interaction()
            await bot.tree.get_command("rival-tools").get_command(name).callback(i, coin, horizon)
            i.response.defer.assert_awaited_once_with(
                ephemeral=True, thinking=True,
            )
            views.append(i.edit_original_response.await_args.kwargs["embed"].to_dict())
        assert views == [views[0], views[0]]
        assert "NOT_PROVEN" in str(views[0])
        assert "No real-money profit" in str(views[0])
        assert shadow.report.call_count == 2
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()



@pytest.mark.asyncio
async def test_ten_source_and_ensemble_discord_commands_are_private_on_android(tmp_path):
    from discord import app_commands

    bot = _client(tmp_path)
    try:
        asset = app_commands.Choice(name="BTC", value="BTC")
        horizon = app_commands.Choice(name="5 minutes", value=5)
        for name, args in (
            ("source-health", (asset,)),
            ("multi-proof", (asset, horizon)),
        ):
            command = bot.tree.get_command("rival-tools").get_command(name)
            assert command is not None
            interaction = _interaction()
            await command.callback(interaction, *args)
            interaction.response.defer.assert_awaited_once_with(
                ephemeral=True, thinking=True,
            )
            interaction.edit_original_response.assert_awaited_once()
            message = interaction.edit_original_response.await_args.kwargs
            assert "disabled" in message["content"]
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_advanced_plain_english_pages_do_not_expose_dev_jargon(tmp_path):
    from unittest.mock import Mock

    bot = _client(tmp_path)
    home = _home(bot)
    try:
        home.navigate("advanced")
        home._buttons()
        assert {"Are My Prices Fresh?", "Is RivaL Improving?",
                "Technical Research"}.issubset(set(_labels(home)))
        assert "rival-profit-proof" not in (await home.render()).description
        assert "/rival-tools" in (await home.render()).description

        home.navigate("feeds")
        home._buttons()
        assert "Refresh" in _labels(home)
        no_collection = await home.render()
        assert "not connected" in no_collection.fields[0].value

        stamp = datetime.now(UTC)
        row = {
            "venue": "hyperliquid", "asset": "BTC", "status": "ok",
            "last_success_at": stamp.isoformat(),
            "last_candle_close": (stamp - timedelta(minutes=5)).isoformat(),
        }
        feed_store = Mock()
        feed_store.feed_health.return_value = [row]
        home.market = feed_store
        home.venues = ("hyperliquid", "binance")
        fresh = await home.render()
        assert "1 of 2 exchanges" in fresh.fields[0].name
        assert "hyperliquid" in fresh.fields[0].value
        assert "binance" in fresh.fields[0].value

        home.navigate("trust")
        home._buttons()
        no_model = await home.render()
        assert "Not verified" in no_model.fields[0].value
        research = Mock()
        research.report.return_value = {
            "settled": 2, "days": 1, "max_sources": 3,
            "brier_improvement": -0.02,
        }
        home.multi = research
        evidence = await home.render()
        assert "NOT beaten the original" in evidence.fields[0].value
        assert "Not yet" in evidence.fields[1].value
        research.report.assert_called_once_with(asset="BTC", horizon_minutes=5)
        assert "Home" in _labels(home)
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


def test_every_legacy_tool_is_grouped_without_duplicate_global_commands(tmp_path):
    bot = _client(tmp_path)
    try:
        roots = {c.name for c in bot.tree.get_commands()}
        assert roots == {"rival", "rival-signal", "rival-stop", "rival-tools"}
        group = bot.tree.get_command("rival-tools")
        names = [c.name for c in group.commands]
        assert len(names) == len(set(names)) == 20
        assert {"paper-open", "paper-close", "practice", "picks",
                "quote", "research", "multi-proof", "source-health",
                "target", "stream-status", "profit-proof"}.issubset(names)
        assert all(not name.startswith("rival-") for name in names)
    finally:
        bot.store.close()
