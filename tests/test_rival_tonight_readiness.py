"""Tonight's RivaL accuracy controls must not disguise missing betting evidence."""
from __future__ import annotations

import discord
import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.readiness import explain_venue_readiness
from parlay_bot.storage.hot import SQLiteHotStore


def test_venue_detail_explains_exact_causal_deficit_without_claiming_profits():
    status = {
        "min_live_bars": 65,
        "streams": [{
            "venue": "hyperliquid", "status": "needs_live_history",
            "observations": 48, "recent_live_streak": 12,
            "causal_5m": 3, "causal_15m": 1,
            "evaluated_5m": 0, "evaluated_15m": 0,
        }],
    }
    text = explain_venue_readiness(status, "hyperliquid")
    assert "12/65" in text
    assert "53" in text
    assert "5m 3" in text
    assert "15m 1" in text
    assert "Backfilled candles never count" in text
    assert "\\n" not in text
    assert "not configured" in explain_venue_readiness(status, "binance")
    status["streams"][0]["status"] = "eligible_shadow"
    status["streams"][0]["recent_live_streak"] = 90
    assert "not real-money approval" in explain_venue_readiness(status, "hyperliquid")


@pytest.mark.asyncio
async def test_accuracy_tab_shows_live_per_exchange_blocker_and_no_unverified_bet(
    tmp_path,
):
    store = SQLiteHotStore(tmp_path / "tonight.sqlite")
    market = PerpetualMarketStore(store)
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("hyperliquid", "dydx", "binance"),
        shadow_enabled=True,
    )
    try:
        view.screen = "accuracy"
        view.section = "venue"
        view._rebuild()
        selects = [
            item for item in view.children
            if isinstance(item, discord.ui.Select)
        ]
        venue_select = next(x for x in selects if x.custom_id == "rival:research:venue")
        assert {x.value for x in venue_select.options} == {
            "hyperliquid", "dydx", "binance",
        }
        assert venue_select.row == 4
        assert view.selected_venue == "hyperliquid"
        embed = await view.render()
        fields = {field.name: field.value for field in embed.fields}
        assert "not real-money approval" not in fields["Why this exchange is or isn't ready"]
        assert "Hyperliquid" not in fields["Why this exchange is or isn't ready"] or "hyperliquid" in fields["Why this exchange is or isn't ready"]
        assert "blocked" in fields["Why this exchange is or isn't ready"].lower()
        assert any(x.custom_id == "rival:research:refresh" for x in view.children)
        assert len(embed.fields) <= 3
        view.section = "overview"
        summary = await view.render()
        assert "NO VERIFIED BETTING EDGE" in next(
            x.value for x in summary.fields
            if x.name == "Tonight's research verdict"
        )
        view.section = "five"
        five = await view.render()
        assert "Issued: **0**" in next(
            x.value for x in five.fields
            if x.name == "Forward 5-minute evaluation"
        )
        view.section = "venue"
        # Selection changes the actual displayed venue without requesting new quotes.
        view.selected_venue = "binance"
        again = await view.render()
        selected_field = next(x.value for x in again.fields if x.name == "Why this exchange is or isn't ready")
        assert "**binance**" in selected_field
    finally:
        view.stop()
        store.close()
