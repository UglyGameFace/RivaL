"""An upstream outage never becomes fabricated odds or unnecessary bot permissions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.kalshi_public import KalshiCryptoBrowser


@pytest.mark.asyncio
async def test_one_kalshi_series_timeout_does_not_drop_other_verified_books():
    calls: list[str] = []
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [
                {"ticker": "KXBTC15M", "title": "Bitcoin price in 15 minutes",
                 "category": "Crypto"},
                {"ticker": "KXBTC1H", "title": "Bitcoin price in one hour",
                 "category": "Crypto"},
            ]})
        if request.url.path.endswith("/markets"):
            if request.url.params.get("series_ticker") == "KXBTC1H":
                raise httpx.ConnectTimeout("provider timeout", request=request)
            return httpx.Response(200, json={"markets": [{
                "ticker": "KXBTC15M-REAL", "title": "BTC market",
                "status": "active", "close_time": (
                    now + timedelta(minutes=15)
                ).isoformat(), "rules_primary": "Published source settlement",
            }]})
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {
                "yes_dollars": [["0.45", "10"]], "no_dollars": [["0.52", "10"]],
            }})
        raise AssertionError(f"Unexpected Kalshi request path {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = KalshiCryptoBrowser(client=client)
        first = await browser.browse("BTC")
        assert first.matched_series == 1
        assert len(first.markets) == 1
        assert first.markets[0].ticker == "KXBTC15M-REAL"
        assert first.markets[0].quote_source == "orderbook"
        assert first.markets[0].yes_ask is not None
        assert "BTC" not in browser._cache

        # Partial discovery was NOT accepted as a complete cached catalog.
        second = await browser.browse("BTC")
        assert second is not first
        assert sum(url.endswith("/series?category=Crypto") for url in calls) == 2


@pytest.mark.asyncio
async def test_all_kalshi_series_timeouts_still_fail_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [
                {"ticker": "KXBTC15M", "title": "Bitcoin price in 15 minutes",
                 "category": "Crypto"},
            ]})
        raise httpx.ConnectTimeout("no official markets", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = KalshiCryptoBrowser(client=client)
        with pytest.raises(ValueError, match="unavailable"):
            await browser.browse("BTC")
        assert not browser._cache


@pytest.mark.asyncio
async def test_discord_guild_intent_without_privileged_content_or_members(tmp_path):
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "guild-intent.sqlite"), discord_token=None,
    ))
    try:
        assert bot.intents.guilds is True
        assert bot.intents.members is False
        assert bot.intents.message_content is False
        assert bot.intents.presences is False
    finally:
        await bot.kalshi_browser.aclose()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
