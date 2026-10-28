"""The /rival-data menu is a navigable dashboard, never a field dump."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from parlay_bot.discord_app.data_dashboard import (
    MarketDataDashboard,
    exchange_embed,
    market_embed,
    overview_embed,
)
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
VENUES = ("hyperliquid", "dydx", "bitget", "gate")
ASSETS = ("BTC", "ETH", "SOL")


def health(
    venue: str, asset: str, *, status: str = "ok", age: int = 2, candles: int = 48,
) -> dict:
    last_success = NOW - timedelta(minutes=age) if status == "ok" else None
    return {
        "venue": venue, "asset": asset, "status": status,
        "last_attempt_at": NOW.isoformat(),
        "last_success_at": last_success.isoformat() if last_success else None,
        "total_new_candles": candles, "last_new_candles": 1,
        "failure_count": 0,
    }


def test_overview_is_compact_and_does_not_count_stale_as_active():
    rows = [
        health("hyperliquid", "BTC"),
        health("hyperliquid", "ETH"),
        health("dydx", "BTC", age=30),
        health("bitget", "BTC", status="blocked"),
    ]
    embed = overview_embed(rows, VENUES, ASSETS, 300, enabled=True, now=NOW)
    fields = {field.name: field.value for field in embed.fields}
    assert "1 / 4" in fields["🌐 Exchanges collecting"]
    assert "2 / 12" in fields["📈 Fresh market feeds"]
    assert "192" in fields["🗃️ Candles saved"]
    assert len(embed.fields) <= 6
    assert "10" in fields["🎯 Source goal"]
    assert "Needs attention" in fields
    assert "not prove forecast accuracy" in embed.footer.text


def test_sources_are_one_exchange_at_a_time_with_human_status():
    rows = [
        health("dydx", "BTC"),
        health("dydx", "ETH", age=60),
        health("dydx", "SOL", status="rate_limited"),
    ]
    embed = exchange_embed(rows, "dydx", ASSETS, 300, now=NOW)
    assert embed.title == "RivaL  |  dYdX"
    assert len(embed.fields) == 3
    assert "🟢 Collecting" in embed.fields[0].value
    assert "🟡 Stale" in embed.fields[1].value
    assert "🔴 Rate Limited" in embed.fields[2].value


def test_market_screen_preserves_per_venue_quotes_and_missing_data():
    rows = [health("hyperliquid", "BTC"), health("dydx", "BTC")]
    latest = {
        "hyperliquid": {
            "close_price": 100.1,
            "closed_at": (NOW - timedelta(minutes=5)).isoformat(),
        },
        "dydx": None,
    }
    embed = market_embed(rows, "BTC", latest, VENUES, 300, now=NOW)
    assert len(embed.fields) == 4
    assert "100.1000 USDC" in embed.fields[0].value
    assert "No stored candle" in embed.fields[1].value
    assert "BTC-USD" in embed.fields[1].name
    assert "BTCUSDT" in embed.fields[2].name
    assert "BTC_USDT" in embed.fields[3].name


class Response:
    def __init__(self) -> None:
        self.deferred = False
        self.denied = None

    async def defer(self) -> None:
        self.deferred = True

    async def send_message(self, message, *, ephemeral):
        self.denied = (message, ephemeral)


class Interaction:
    def __init__(self, user_id=42) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = Response()
        self.embed = None
        self.view = None

    async def edit_original_response(self, *, embed, view):
        self.embed = embed
        self.view = view


def buttons(view: MarketDataDashboard) -> dict[str, discord.ui.Item]:
    return {
        item.custom_id: item for item in view.children
        if getattr(item, "custom_id", None) is not None
    }


@pytest.mark.asyncio
async def test_menu_navigation_edits_same_private_screen_and_has_no_dead_controls():
    dashboard = MarketDataDashboard(
        owner_id=42, market=None, venues=VENUES, assets=ASSETS,
        interval_seconds=300, enabled=False,
    )
    assert len(buttons(dashboard)) == 4
    assert buttons(dashboard)["rival:data:overview"].disabled
    interaction = Interaction()
    await dashboard.show(interaction, "exchanges")
    assert interaction.response.deferred
    assert interaction.view is dashboard
    assert "dYdX" not in interaction.embed.title
    assert "rival:data:choose:exchange" in buttons(dashboard)
    assert buttons(dashboard)["rival:data:exchanges"].disabled

    interaction2 = Interaction()
    await dashboard.show(interaction2, "markets")
    assert "rival:data:choose:market" in buttons(dashboard)
    assert "rival:data:choose:exchange" not in buttons(dashboard)
    assert "BTC" in interaction2.embed.title
    assert interaction2.view is dashboard

    other = Interaction(user_id=99)
    assert not await dashboard.interaction_check(other)
    assert other.response.denied[1] is True
    assert await dashboard.interaction_check(Interaction())


@pytest.mark.asyncio
async def test_dashboard_reads_real_market_store_for_market_screen(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    opened = NOW - timedelta(minutes=5)
    candle = MarketCandle(
        "hyperliquid", "BTC", "USDC", 5, opened,
        NOW - timedelta(milliseconds=1), NOW + timedelta(seconds=2),
        100, 105, 95, 102.5, 8, 20,
    )
    batch = MarketBatch((), (candle,), 5, NOW + timedelta(seconds=2))
    try:
        assert market.ingest(batch).inserted_candles == 1
        market.record_feed_health(
            venue="hyperliquid", asset="BTC", status="ok",
            attempted_at=NOW + timedelta(seconds=3),
            new_candles=1, total_candles=1,
        )
        dashboard = MarketDataDashboard(
            owner_id=42, market=market,
            venues=("hyperliquid", "dydx"), assets=("BTC",),
            interval_seconds=300, enabled=True,
        )
        assert "Candles saved" in (await dashboard.render()).to_dict()["fields"][2]["name"]
        dashboard.screen = "markets"
        dashboard._rebuild()
        embed = await dashboard.render()
        assert "102.5000 USDC" in embed.fields[0].value
        assert "No stored candle" in embed.fields[1].value
    finally:
        store.close()


def test_disabled_dashboard_never_claims_live_collection():
    embed = overview_embed([], VENUES, ASSETS, 300, enabled=False, now=NOW)
    assert "disabled" in embed.description
    assert len(embed.fields) == 0


@pytest.mark.asyncio
async def test_ten_venue_market_menu_has_four_entries_per_page_and_working_navigation():
    venues = (
        "hyperliquid", "dydx", "bitget", "gate", "okx",
        "kucoin", "deribit", "binance", "lighter", "phemex",
    )
    dashboard = MarketDataDashboard(
        owner_id=42, market=None, venues=venues, assets=("BTC", "ETH", "SOL"),
        interval_seconds=300, enabled=False,
    )
    first = Interaction()
    await dashboard.show(first, "markets")
    assert len(first.embed.fields) == 4
    assert "Page 1/3" in first.embed.description
    assert buttons(dashboard)["rival:data:previous"].disabled
    assert not buttons(dashboard)["rival:data:next"].disabled

    second = Interaction()
    await buttons(dashboard)["rival:data:next"].callback(second)
    assert second.response.deferred
    assert second.view is dashboard
    assert "Page 2/3" in second.embed.description
    assert len(second.embed.fields) == 4
    assert second.embed.fields[0].name.startswith("OKX")

    third = Interaction()
    await buttons(dashboard)["rival:data:next"].callback(third)
    assert len(third.embed.fields) == 2
    assert "Page 3/3" in third.embed.description
    assert buttons(dashboard)["rival:data:next"].disabled
    assert third.embed.fields[-1].name.startswith("Phemex")

    prior = Interaction()
    await buttons(dashboard)["rival:data:previous"].callback(prior)
    assert "Page 2/3" in prior.embed.description

    await dashboard.show(Interaction(), "overview")
    again = Interaction()
    await dashboard.show(again, "markets")
    assert "Page 1/3" in again.embed.description
    assert len(again.embed.fields) == 4
