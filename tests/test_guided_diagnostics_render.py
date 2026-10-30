"""Render contracts for the new diagnostics fields (sports pause, PASS reasons,
system health). Thin wiring tests: the counters themselves are unit-tested
elsewhere; these prove the fields appear and never break the menu."""
from __future__ import annotations

import pytest

from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from tests.test_guided_home import _client, _home


def _fields(embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


@pytest.mark.asyncio
async def test_feeds_page_shows_system_health_when_nothing_recorded(tmp_path) -> None:
    bot = _client(tmp_path)
    home = _home(bot)
    market = PerpetualMarketStore(bot.store)
    market.initialize()
    home.market = market
    home.collector = CurrentBoardCollector(client=None, store=bot.store)
    home.venues = ("hyperliquid",)
    home.page = "feeds"
    try:
        embed = await home.render()
        fields = _fields(embed)
        assert "System health" in fields
        health = fields["System health"]
        assert "Crypto ingestion: no successful ingestion recorded" in health
        assert "Forecast cycle: no cycle recorded yet" in health
        assert "Sports refresh: no completed refresh yet" in health
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_slip_pass_shows_builder_reasons(tmp_path) -> None:
    bot = _client(tmp_path)
    home = _home(bot)
    home.page = "slip"
    home.slip = None
    bot.parlay_builder.last_diagnostics = {
        "rows_seen": 4,
        "below_advertised_edge": 2,
        "negative_edge": 2,
        "insufficient_reference_books": 0,
    }
    try:
        embed = await home.render()
        fields = _fields(embed)
        assert "PASS" in embed.description
        assert "Why PASS" in fields
        assert "below the 2% advertised-edge screen" in fields["Why PASS"]
        assert "negative edge" in fields["Why PASS"]
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_sports_page_shows_persisted_pause_notice(tmp_path) -> None:
    bot = _client(tmp_path)
    home = _home(bot)
    home.page = "sports"
    # The test client leaves the sports collector unstarted; the render only
    # reads refresh state, so a bare collector over the same store suffices.
    home.collector = CurrentBoardCollector(client=None, store=bot.store)
    home.collector.store.set_runtime_state(
        "sportsgameodds:last_refresh_notice",
        "SportsGameOdds monthly entity reserve protected: "
        "250 entities remain; reserve is 250",
    )
    try:
        embed = await home.render()
        fields = _fields(embed)
        assert "Sports collection paused" in fields
        assert "reserve" in fields["Sports collection paused"]
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
