"""No default betting market; accuracy evidence and actions stay source-matched."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_guided_home import _client, _home, _interaction, _labels


@pytest.mark.asyncio
async def test_crypto_entry_starts_with_analysis_and_no_implicit_market(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        assert home.live_source is None
        event = _interaction()
        await home.act(event, "crypto-desk")
        assert home.page == "crypto-desk"
        assert home.live_source is None
        home._buttons()  # Real Discord receives a replacement view after navigation.
        assert "View Contract Call" not in _labels(home)
        assert "Track Market" not in _labels(home)
        assert "Get Exchange Forecast" not in _labels(home)
        assert "Check Kalshi Contracts" not in _labels(home)
        market = next(
            item for item in home.children
            if item.custom_id == "rival:guided:market"
        )
        assert all(not option.default for option in market.options)
        embed = await home.render()
        assert "Analysis" in embed.title
        assert "Not selected" in embed.description
        assert "Start here" in [field.name for field in embed.fields]
        # No tracking or AI call can start while the market is unselected.
        await home.act(event, "desk-call")
        assert "Choose a market first" in home.note
        assert home.page == "crypto-desk"
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_source_choice_changes_calls_and_does_not_substitute_contracts(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        home.navigate("crypto-desk")
        home.live_source = "kalshi"
        home._buttons()
        assert "Check Kalshi Contracts" in _labels(home)
        assert "Track Market" not in _labels(home)
        assert "Follow Forecast" not in _labels(home)
        embed = await home.render()
        assert "No validated Kalshi-specific RivaL model" in str(embed.to_dict())
        await home.act(_interaction(), "desk-call")
        assert home.page == "live"
        assert "Kalshi" in (await home.render()).title

        home.navigate("crypto-desk")
        home.live_source = "exchange"
        home._buttons()
        assert "Get Exchange Forecast" in _labels(home)
        assert "Follow Forecast" in _labels(home)
        assert "View Contract Call" not in _labels(home)
        home.targets.follow = AsyncMock(return_value=(True, "Watching exchange forecast"))
        await home.act(_interaction(), "desk-track")
        home.targets.follow.assert_awaited_once()
        assert "Watching exchange forecast" in home.note
        assert home.page == "crypto-desk"
    finally:
        home.stop()
        await bot.kalshi_browser.aclose()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_selected_polymarket_tracks_only_explicit_contract(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    tracker = SimpleNamespace(start=AsyncMock(return_value=(True, "Tracking only this market")))
    home.auto_hunt = SimpleNamespace(bot=SimpleNamespace(polymarket_board=tracker))
    try:
        home.navigate("crypto-desk")
        home.live_source = "polymarket"
        home.coin = "ETH"
        home.horizon = 15
        home._buttons()
        assert "View Contract Call" in _labels(home)
        assert "Track Market" in _labels(home)
        assert "Get Exchange Forecast" not in _labels(home)
        await home.act(_interaction(), "desk-track")
        tracker.start.assert_awaited_once()
        call = tracker.start.await_args
        assert call.kwargs == {"asset": "ETH", "horizon": 15}
        assert "Tracking only this market" in home.note
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_legacy_live_button_cannot_auto_pick_polymarket(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        await home.act(_interaction(), "live")
        assert home.page == "crypto-desk"
        assert home.live_source is None
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_platform_choice_explains_perpetuals_and_draftkings_without_fake_calls(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        home.navigate("crypto-desk")
        home._buttons()
        selector = next(
            x for x in home.children if x.custom_id == "rival:guided:market"
        )
        assert [o.value for o in selector.options] == [
            "polymarket", "exchange", "kalshi", "draftkings",
        ]
        assert "DraftKings" in next(
            o.description for o in selector.options if o.value == "exchange"
        )
        start = await home.render()
        text = "\n".join(x.value for x in start.fields)
        assert "DraftKings Predictions" in text
        assert "Not DraftKings" in text
        assert "Hyperliquid" in text
        assert "Exchange forecast results" not in str(start.to_dict())

        home.live_source = "draftkings"
        home._buttons()
        assert any(
            getattr(x, "url", None) == "https://predictions.draftkings.com/en"
            for x in home.children
        )
        assert "View Contract Call" not in _labels(home)
        assert "Get Exchange Forecast" not in _labels(home)
        assert "Follow Forecast" not in _labels(home)
        assert any(x.custom_id == "rival:guided:coin" for x in home.children)
        assert any(x.custom_id == "rival:guided:horizon" for x in home.children)
        assert "Independent AI" in _labels(home)
        assert "Find My Coin" in _labels(home)
        assert "Check CDNA Quote" in _labels(home)
        # Old Discord components can still be clicked after switching the
        # selected provider. They must never borrow a Polymarket signal.
        await home.act(_interaction(), "desk-call")
        assert home.page == "crypto-desk"
        assert "not connected" in home.note
        draft = await home.render()
        draft_text = str(draft.to_dict())
        assert "not connected" in home.note
        assert "No verified DraftKings" in draft_text
        assert "Exchange forecast results" not in draft_text

        home.live_source = "exchange"
        exchange = await home.render()
        exchange_text = str(exchange.to_dict())
        assert "Crypto perpetual futures" in exchange_text
        assert "not** matched Polymarket" in exchange_text
        assert "Polymarket model progress" not in exchange_text
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_dashboard_shows_polymarket_outcomes_without_market_choice(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        home.navigate("performance")
        assert home.live_source is None
        home._buttons()
        assert all(x.custom_id != "rival:guided:market" for x in home.children)
        home.polymarket_status = lambda asset, horizon: {
            "observed_contracts": 99,
            "learned_contracts": 99,
            "settled_forward_contracts": 0,
            "forward_brier": None,
            "market_baseline_brier": None,
        }
        pane = await home.render()
        fields = {x.name: x.value for x in pane.fields}
        assert "99" in fields["🎯 Polymarket"]
        assert "0 settled calls" in fields["🎯 Polymarket"]
        assert "no paired score available" in fields["🎯 Polymarket"]
        assert "Choose a market" not in str(pane.to_dict())
        assert len(pane.fields) == 4

        home.polymarket_status = None
        offline = await home.render()
        assert "Contract score read unavailable" in str(offline.to_dict())
        assert "0% win rate" not in str(offline.to_dict())
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_polymarket_stats_do_not_require_exchange_or_drive(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    try:
        home.navigate("performance")

        def unrelated_training(_asset, _horizon):
            raise RuntimeError("Unrelated exchange/Drive query failed")

        home.training_status = unrelated_training
        home.polymarket_status = lambda _asset, _horizon: {
            "observed_contracts": 18,
            "learned_contracts": 12,
            "settled_forward_contracts": 5,
            "forward_brier": .21,
            "market_baseline_brier": .24,
        }
        view = await home.render()
        body = str(view.to_dict())
        assert "12 historical outcomes (not forward calls)" in body
        assert "5 settled calls" in body
        assert "0.2100" in body
        assert "0.2400" in body
        assert "Learning heartbeat: read unavailable" in body
        home.navigate("trust")
        assert home.page == "performance"
        detail = await home.render()
        assert len(detail.fields) == 4
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_exchange_performance_one_page_shows_sample_and_learning(
    tmp_path, monkeypatch,
):
    from parlay_bot.discord_app import home as home_module

    bot = _client(tmp_path)
    home = _home(bot)
    try:
        class Audit:
            def summary(self, *, asset):
                assert asset == "BTC"
                recent = {
                    "issued": 4159, "settled": 4150, "decided": 4150,
                    "correct": 2063, "hit_rate": 2063/4150,
                    "brier": .251, "baseline_brier": .249,
                    "model_beats_baseline": False,
                }
                return {"last_7d": {5: recent}, "prior_7d": {5: {"hit_rate": .52}}}

            def learning_lineage_summary(self, *, asset, horizon_minutes):
                assert (asset, horizon_minutes) == ("BTC", 5)
                return {
                    "forecasts": 100, "lineage_proven": 100,
                    "lineage_unknown": 0, "historically_seeded": 14,
                }

        monkeypatch.setattr(
            home_module, "ForwardAccuracyMonitor", lambda _market: Audit(),
        )
        home.market = object()
        from datetime import UTC, datetime

        # Training registry/Drive queries must never be called by the overview.
        home.training_status = lambda *_: (_ for _ in ()).throw(
            AssertionError("Performance invoked expensive training read")
        )
        monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
            "learning": {
                "status": "recorded", "checked_at": datetime.now(UTC),
                "math_ok": True, "model_review_ok": True,
                "issued": 4, "settled": 7,
            },
            "forecast": None,
        })
        home.polymarket_status = lambda _asset, _horizon: {
            "learned_contracts": 99, "settled_forward_contracts": 0,
            "forward_brier": None, "market_baseline_brier": None,
        }
        home.navigate("performance")
        home._buttons()
        assert "Refresh" in _labels(home)
        assert "Learning Details" not in _labels(home)
        assert "Data Health" not in _labels(home)
        assert len(home.children) <= 25
        assert max(x.row for x in home.children) <= 4
        pane = await home.render()
        body = str(pane.to_dict())
        assert len(pane.fields) == 4
        assert "4,159" in body and "4,150" in body
        assert "2,063/4,150" in body and "49.7%" in body
        assert "0.2510" in body and "0.2490" in body
        assert "99" in body and "no paired score available" in body
        assert "14" in body and "Historical seeds in recent verified lineage" in body
        assert "4 issued / 7 settled" in body
        assert "NO VERIFIED TRADING EDGE" in body
        assert "14d comparison: read unavailable" in body
        assert "practice usdt" not in body.lower()
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
