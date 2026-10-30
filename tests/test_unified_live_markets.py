"""One live source-focused market screen, with no guessed odds or duplicate reads."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.home import GuidedHome
from parlay_bot.discord_app.kalshi_public import KalshiMarket, KalshiSnapshot
from parlay_bot.discord_app.polymarket_watch import (
    PolymarketListedWindow,
    PolymarketNotice,
    PolymarketReader,
    PolymarketWindowBoard,
    market_slug,
)
from parlay_bot.perpetuals.polymarket_research import ContractCall


def _home(bot):
    return GuidedHome(
        owner_id=31337, location=bot.location_service,
        builder=bot.parlay_builder, collector=None, market=None,
        targets=bot.direction_targets, paper=bot.paper_ledger,
        quotes=bot.public_quotes, profit=bot.quote_profit,
        kalshi=bot.kalshi_browser,
    )


@pytest.mark.asyncio
async def test_live_desk_shows_actual_verified_market_and_switches_provider_in_place(
    tmp_path,
):
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "live.sqlite"), discord_token=None,
    ))
    home = _home(bot)
    now = datetime.now(UTC)
    start = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    slug = market_slug("BTC", 5, start)
    current = PolymarketListedWindow(
        asset="BTC", horizon=5, slug=slug,
        opens=start, closes=start+timedelta(minutes=5),
        accepting_orders=True, price_to_beat=76000.0,
    )
    nxt = PolymarketListedWindow(
        asset="BTC", horizon=5,
        slug=market_slug("BTC", 5, start+timedelta(minutes=5)),
        opens=start+timedelta(minutes=5),
        closes=start+timedelta(minutes=10),
        accepting_orders=False, price_to_beat=None,
    )
    notice = PolymarketNotice(
        asset="BTC", horizon=5, issued=start,
        expires=current.closes, slug=slug,
        up_ask=0.61, down_ask=0.41, quote_at=now,
    )
    board = PolymarketWindowBoard(
        checked_at=now, current=current, next_window=nxt,
        current_quote=notice,
    )
    research = ContractCall(
        slug=slug, asset="BTC", horizon=5, direction="UP", issued_at=now,
        expires=current.closes, model="free-test", thesis="Verified contract research",
        risk="Unvalidated", evidence_sha256="a" * 64,
    )
    service = SimpleNamespace(
        store=SimpleNamespace(get=lambda *_: None),
        preview=AsyncMock(return_value=(board, research)),
    )
    home.auto_hunt = SimpleNamespace(
        bot=SimpleNamespace(polymarket_board=service),
    )
    try:
        home.navigate("live")
        home._buttons()
        poly = await home.render()
        assert "Crypto Markets" in poly.title
        assert "Polymarket Global" in poly.title
        summary = "\n".join(f.value for f in poly.fields)
        assert "76000" in summary or "76,000" in summary
        assert "UP ask $0.61" in summary
        assert "DOWN ask $0.41" in summary
        assert "Next verified contract" in "\n".join(f.name for f in poly.fields)
        assert "**UP**" in summary
        assert "exchange-candle lean" not in summary
        assert "Verified contract research" in summary
        assert "Polymarket AI: UP" in poly.description
        assert "Browse Kalshi" not in [
            x.label for x in home.children if hasattr(x, "label")
        ]

        # A provider change is NOT a new view/page or an order submission.
        home.live_source = "kalshi"
        home._buttons()
        bot.kalshi_browser.browse = AsyncMock(return_value=KalshiSnapshot(
            asset="BTC", checked_at=now, matched_series=1,
            markets=(KalshiMarket(
                ticker="KXBTC-TEST", title="Bitcoin above the threshold?",
                close_time=now+timedelta(hours=1),
                yes_ask=0.4, no_ask=0.61, rules="Separate Kalshi rule",
            ),),
        ))
        other = await home.render()
        assert home.page == "live"
        assert "Kalshi" in other.title
        assert "KXBTC-TEST" in "\n".join(x.value for x in other.fields)
        assert "Separate Kalshi rule" in "\n".join(x.value for x in other.fields)
        assert "Polymarket" not in other.title
        assert "Live contract call: WAIT" in other.description
        home.live_source = "draftkings"
        dk = await home.render()
        assert "Not connected" in "\n".join(x.value for x in dk.fields)
        bot.kalshi_browser.browse.assert_awaited_once()
        assert service.preview.await_count == 1
    finally:
        home.stop()
        await bot.kalshi_browser.aclose()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_live_polymarket_display_book_deduplicates_but_never_reuses_next_window():
    reader = PolymarketReader()
    stamp = datetime.fromtimestamp(
        int(datetime.now(UTC).timestamp()) // 300 * 300 + 30, UTC,
    )
    issued = stamp.replace(second=0, microsecond=0)
    slug = market_slug("BTC", 5, stamp)
    notice = PolymarketNotice(
        asset="BTC", horizon=5,
        issued=issued, expires=issued+timedelta(minutes=5),
        slug=slug, up_ask=0.45, down_ask=0.55, quote_at=stamp,
    )
    reader._lookup = AsyncMock(return_value=notice)
    one = await reader.market("BTC", 5, now=stamp, display_only=True)
    two = await reader.market(
        "BTC", 5, now=stamp+timedelta(seconds=3), display_only=True,
    )
    assert one is two
    assert reader._lookup.await_count == 1
    # Re-use only the same contract; refresh after 5 seconds.
    await reader.market(
        "BTC", 5, now=stamp+timedelta(seconds=6), display_only=True,
    )
    assert reader._lookup.await_count == 2
    await reader.market(
        "BTC", 5, now=stamp+timedelta(minutes=5), display_only=True,
    )
    assert reader._lookup.await_count == 3


@pytest.mark.asyncio
async def test_concurrent_gamma_listing_uses_one_shared_request(monkeypatch):
    now = datetime.now(UTC)
    opened = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    close = opened + timedelta(minutes=5)
    slug = market_slug("BTC", 5, opened)
    requests = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        return httpx.Response(200, json={
            "slug": slug, "active": True, "closed": False,
            "archived": False,
            "eventStartTime": opened.isoformat(),
            "endDate": close.isoformat(),
            "markets": [{
                "eventStartTime": opened.isoformat(),
                "active": True, "closed": False,
                "acceptingOrders": True, "enableOrderBook": True,
                "outcomes": '["Up","Down"]',
            }],
        })

    actual_client = httpx.AsyncClient

    def client(*args, **kwargs):
        return actual_client(
            transport=httpx.MockTransport(handle),
            timeout=kwargs.get("timeout", 3.5),
            follow_redirects=False,
        )

    monkeypatch.setattr(
        "parlay_bot.discord_app.polymarket_watch.httpx.AsyncClient", client,
    )
    reader = PolymarketReader()
    left, right = await asyncio.gather(
        reader.listed("BTC", 5, opened, now=now),
        reader.listed("BTC", 5, opened, now=now),
    )
    assert left is not None
    assert right == left
    assert len(requests) == 1
