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
async def test_legacy_feed_link_shows_real_cycle_health_on_one_page(tmp_path) -> None:
    bot = _client(tmp_path)
    home = _home(bot)
    market = PerpetualMarketStore(bot.store)
    market.initialize()
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor

    ForwardAccuracyMonitor(market).initialize()
    home.market = market
    home.collector = CurrentBoardCollector(client=None, store=bot.store)
    home.venues = ("hyperliquid",)
    home.navigate("feeds")  # Old link now opens the one-page scorecard.
    try:
        assert home.page == "performance"
        embed = await home.render()
        fields = _fields(embed)
        assert len(fields) == 4
        health = fields["📡 System status"]
        assert "Feeds: **0/1** fresh" in health
        assert "Forecast cycle: **no cycle recorded**" in health

        from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor

        ForwardAccuracyMonitor(market).run_once()
        recorded = await home.render()
        after = _fields(recorded)["📡 System status"]
        assert "Forecast check:" in after
        assert "**0** issued" in after
        assert "no cycle recorded" not in after
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


@pytest.mark.asyncio
async def test_feed_read_failure_does_not_hide_recorded_cycle(tmp_path, monkeypatch) -> None:
    """A slow/broken feed must not erase the separately persisted engine status."""
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor

    bot = _client(tmp_path)
    home = _home(bot)
    market = PerpetualMarketStore(bot.store)
    market.initialize()
    ForwardAccuracyMonitor(market).run_once()
    home.market = market
    home.venues = ("hyperliquid",)
    home.navigate("performance")

    def broken_feed():
        raise RuntimeError("simulated feed database failure")

    monkeypatch.setattr(market, "feed_health", broken_feed)
    try:
        embed = await home.render()
        status = _fields(embed)["📡 System status"]
        assert "Feeds: **read unavailable**" in status
        assert "Forecast check:" in status
        assert "**0** issued" in status
        assert len(embed.fields) == 4
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
