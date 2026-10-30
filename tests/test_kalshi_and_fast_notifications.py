"""UI navigation and public market discovery do not block on sports/DB work."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.home import GuidedHome
from parlay_bot.discord_app.kalshi_public import (
    KalshiCryptoBrowser, matching_series, parse_markets,
)


@pytest.mark.asyncio
async def test_notification_options_does_not_call_database_or_redraw_lock(
    tmp_path,
):
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "db.sqlite"), discord_token=None,
    ))
    home = GuidedHome(
        owner_id=123, location=bot.location_service,
        builder=bot.parlay_builder, collector=None, market=None,
        targets=bot.direction_targets, paper=bot.paper_ledger,
        quotes=bot.public_quotes, profit=None,
        auto_hunt=bot.auto_hunt, kalshi=bot.kalshi_browser,
    )
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123), response=response,
        edit_original_response=AsyncMock(),
    )
    try:
        home.navigate("live")
        # If the navigation tries to acquire the old redraw lock it would
        # deadlock: the test holds it across the entire callback.
        async with home._redraw_lock:
            await home.act(interaction, "hunt-tools")
        response.send_message.assert_awaited_once()
        args = response.send_message.await_args.kwargs
        assert args["ephemeral"] is True
        assert args["view"].page == "hunt-tools"
        assert "Optional Alert Types" in args["embed"].title
        assert home.page == "live"  # original tracker is untouched
        interaction.edit_original_response.assert_not_awaited()
        assert "Polymarket Global" in (await home.render()).title
    finally:
        home.stop()
        await bot.kalshi_browser.aclose()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


def test_kalshi_series_uses_exact_crypto_symbol_not_unrelated_string():
    result = matching_series({"series": [
        {"ticker": "KXBTC15M", "title": "Bitcoin price in 15 minutes",
         "category": "Crypto"},
        {"ticker": "KXETH", "title": "Ethereum price", "category": "Crypto"},
        {"ticker": "KXNOTBTC", "title": "BTC sports", "category": "Sports"},
    ]}, asset="BTC")
    assert result == ("KXBTC15M",)


def test_kalshi_public_open_markets_are_returned_as_active():
    now = datetime(2026, 10, 30, tzinfo=UTC)
    markets = parse_markets({"markets": [
        {"ticker": "KXBTC15M-123", "title": "Bitcoin price rises",
         "status": "active", "close_time": (now+timedelta(minutes=15)).isoformat(),
         "yes_ask_dollars": "0.43", "no_ask_dollars": "0.58",
         "rules_primary": "Provider-specific settlement"},
        {"ticker": "KXBTC15M-OLD", "title": "Old", "status": "settled",
         "close_time": (now+timedelta(minutes=15)).isoformat()},
    ]}, now=now)
    assert len(markets) == 1
    assert markets[0].yes_ask == 0.43
    assert markets[0].no_ask == 0.58
    assert markets[0].rules == "Provider-specific settlement"


@pytest.mark.asyncio
async def test_kalshi_market_browser_is_on_demand_and_does_not_guess_markets():
    calls = []

    def responder(req):
        calls.append(req)
        if req.url.path.endswith("/series"):
            assert req.url.params.get("category") == "Crypto"
            return httpx.Response(200, json={"series": [
                {"ticker": "KXBTC15M", "title": "Bitcoin 15m",
                 "category": "Crypto"},
            ]})
        assert req.url.params.get("series_ticker") == "KXBTC15M"
        return httpx.Response(200, json={"markets": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
    ) as client:
        browser = KalshiCryptoBrowser(client=client)
        assert calls == []
        first = await browser.browse("BTC")
        assert first.markets == ()
        assert first.matched_series == 1
        assert len(calls) == 2
        second = await browser.browse("BTC")
        assert first is second
        assert len(calls) == 2  # cache, no second provider scan


@pytest.mark.asyncio
async def test_kalshi_provider_errors_fail_closed_no_unverified_prices():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, json={"error": "quota"}),
        ),
    ) as client:
        browser = KalshiCryptoBrowser(client=client)
        with pytest.raises(ValueError, match="unavailable"):
            await browser.browse("BTC")
