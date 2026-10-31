"""Owner-only CDNA public research, fully isolated from model/trading paths."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.cdna_public import (
    CDNAPersonalReader,
    CDNAUnavailable,
    parse_cdna_price,
)
from parlay_bot.discord_app.cdna_ui import CDNAPersonalModal


def payload(*, symbol="BTC-2026", bid="0.45", ask="0.54", status="active",
            at=None):
    at = at or datetime.now(UTC)
    return {"data": {
        "symbol": symbol, "title": "Bitcoin threshold",
        "bid": bid, "ask": ask, "status": status,
        "updated_at": at.isoformat(),
    }}


def test_cdna_parser_exact_symbol_and_freshness_are_not_model_probabilities():
    now = datetime.now(UTC)
    price = parse_cdna_price(payload(at=now), requested="BTC-2026", checked_at=now)
    assert price.fresh
    assert price.bid == .45 and price.ask == .54
    assert not hasattr(price, "model_probability")
    assert not parse_cdna_price(
        payload(at=now-timedelta(minutes=10)),
        requested="BTC-2026", checked_at=now,
    ).fresh
    with pytest.raises(CDNAUnavailable, match="different"):
        parse_cdna_price(payload(), requested="ETH-2026", checked_at=now)
    with pytest.raises(CDNAUnavailable, match="bid/ask"):
        parse_cdna_price(payload(bid="0.60", ask="0.50"),
                         requested="BTC-2026", checked_at=now)
    with pytest.raises(CDNAUnavailable, match="bid/ask"):
        parse_cdna_price(payload(bid="nan"), requested="BTC-2026", checked_at=now)
    with pytest.raises(CDNAUnavailable, match="UTC offset"):
        parse_cdna_price(
            payload(at=now.replace(tzinfo=None)),
            requested="BTC-2026", checked_at=now,
        )


@pytest.mark.asyncio
async def test_personal_quotes_on_demand_no_db_ai_storage_and_rate_limit():
    calls = []
    def responder(req: httpx.Request):
        calls.append(str(req.url))
        assert req.method == "GET"
        assert req.url.path == "/api/v1/predictions/contracts/BTC-2026/price"
        assert "X-API-Key" not in req.headers
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        reader = CDNAPersonalReader(client=client)
        assert calls == []
        quote = await reader.quote("BTC-2026")
        assert quote.fresh
        assert len(calls) == 1
        assert not hasattr(reader, "store")
        assert not hasattr(reader, "ai")
        with pytest.raises(CDNAUnavailable, match="retry after"):
            await reader.quote("BTC-2026")
        assert len(calls) == 1
        with pytest.raises(CDNAUnavailable, match="real CDNA"):
            await reader.quote("../secret")
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_cdna_429_fails_closed_and_starts_quiet_circuit_breaker():
    calls = []
    def responder(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        reader = CDNAPersonalReader(client=client)
        with pytest.raises(CDNAUnavailable, match="rate limited"):
            await reader.quote("BTC-2026")
        with pytest.raises(CDNAUnavailable, match="paused"):
            await reader.quote("SOL-2026")
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_cdna_search_uses_documented_event_and_contract_endpoints_only():
    uid = "123e4567-e89b-12d3-a456-426614174000"
    requests = []
    def responder(req):
        requests.append(str(req.url))
        if req.url.path.endswith("/events/search"):
            assert req.url.params["q"] == "BTC"
            return httpx.Response(200, json={"data": [{
                "id": uid, "title": "Bitcoin future", "status": "active",
            }]})
        assert req.url.path.endswith(f"/events/{uid}/contracts")
        return httpx.Response(200, json={"data": [
            {"symbol": "BTC-2026", "title": "BTC above threshold"},
            {"symbol": "../bad", "title": "Bad id"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        reader = CDNAPersonalReader(client=client)
        found = await reader.search_contracts("BTC")
        assert found == (("BTC-2026", "BTC above threshold", "Bitcoin future"),)
        assert len(requests) == 2
        with pytest.raises(CDNAUnavailable, match="retry after"):
            await reader.search_contracts("BTC")
        assert len(requests) == 2


@pytest.mark.asyncio
async def test_cdna_modal_denies_nonowner_before_network():
    reader = SimpleNamespace(quote=AsyncMock(), search_contracts=AsyncMock())
    modal = CDNAPersonalModal(owner_id=42, reader=reader, search=False)
    modal.input._value = "BTC-2026"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=77),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await modal.on_submit(interaction)
    interaction.response.send_message.assert_awaited_once()
    reader.quote.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdna_modal_denies_other_app_users_before_provider():
    reader = SimpleNamespace(quote=AsyncMock(), search_contracts=AsyncMock())
    modal = CDNAPersonalModal(owner_id=42, reader=reader, search=True)
    modal.input._value = "BTC"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42),
        response=SimpleNamespace(send_message=AsyncMock()),
        client=SimpleNamespace(application_info=AsyncMock(
            return_value=SimpleNamespace(owner=SimpleNamespace(id=99)),
        )),
    )
    await modal.on_submit(interaction)
    reader.search_contracts.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_draftkings_assisted_menu_reuses_existing_ai_without_cdna_data(tmp_path):
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"), discord_token=None,
    ))
    home = bot._build_guided_home(42)
    try:
        home.navigate("crypto-desk")
        home.live_source = "draftkings"
        home.coin = "SOL"
        home.horizon = 60
        home._buttons()
        labels = [x.label for x in home.children if hasattr(x, "label")]
        assert "Find My Coin" in labels
        assert "Check CDNA Quote" in labels
        assert "Independent AI" in labels
        assert "Open DraftKings Predictions" in labels
        assert any(x.custom_id == "rival:guided:coin" for x in home.children)
        assert any(x.custom_id == "rival:guided:horizon" for x in home.children)
        assert home.cdna_personal is bot.cdna_personal
        assert home._display_generation().cdna_personal is bot.cdna_personal
        screen = await home.render()
        text = str(screen.to_dict())
        assert "No verified DraftKings" in text
        assert "NOT" in text
        assert "Independent AI" in text
        assert "contract" in text.lower()
        assert "Market" not in getattr(home, "provider_quote", {})
        # A prior manual quote can never trigger automatic AI/forecast calls.
        assert not hasattr(home, "cdna_quote_cache")
    finally:
        home.stop()
        await bot.cdna_personal.aclose()
        await bot.kalshi_browser.aclose()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


@pytest.mark.asyncio
async def test_one_click_coin_discovery_returns_only_exact_asset_and_two_quotes():
    uid = "123e4567-e89b-12d3-a456-426614174000"
    calls = []
    def responder(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/events/search"):
            assert req.url.params["q"] == "BTC"
            return httpx.Response(200, json={"data": [{
                "id": uid, "title": "Bitcoin event",
            }]})
        if req.url.path.endswith("/contracts"):
            return httpx.Response(200, json={"data": [
                {"symbol": "ABTC9", "title": "Not BTC"},
                {"symbol": "ETH-2026", "title": "Ethereum only"},
                {"symbol": "BTC-2026", "title": "Bitcoin above threshold"},
                {"symbol": "BTC-2027", "title": "BTC threshold next year"},
                {"symbol": "BTC-2028", "title": "BTC third"},
            ]})
        if req.url.path.endswith("/BTC-2026/price"):
            return httpx.Response(200, json=payload(symbol="BTC-2026"))
        if req.url.path.endswith("/BTC-2027/price"):
            return httpx.Response(200, json=payload(symbol="BTC-2027"))
        raise AssertionError("unapproved/extra CDNA request: " + req.url.path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        reader = CDNAPersonalReader(client=client)
        found = await reader.discover_coin("BTC")
        assert [x[0] for x in found] == ["BTC-2026", "BTC-2027"]
        assert all(x[3].fresh for x in found)
        assert len(calls) == 4
        with pytest.raises(CDNAUnavailable, match="Choose BTC"):
            await reader.discover_coin("DOGE")
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_coin_search_results_dont_invent_cdna_price_for_missing_book():
    uid = "123e4567-e89b-12d3-a456-426614174000"
    def responder(req):
        if req.url.path.endswith("/events/search"):
            return httpx.Response(200, json={"data": [{
                "id": uid, "title": "Ethereum event",
            }]})
        if req.url.path.endswith("/contracts"):
            return httpx.Response(200, json={"data": [{
                "symbol": "ETH-2026", "title": "Ethereum conditional",
            }]})
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        reader = CDNAPersonalReader(client=client)
        found = await reader.discover_coin("ETH")
        assert len(found) == 1
        assert found[0][3] is None


@pytest.mark.asyncio
async def test_one_click_owner_auth_blocks_all_provider_calls():
    from parlay_bot.discord_app.cdna_ui import verify_cdna_owner
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        client=SimpleNamespace(application_info=AsyncMock()),
    )
    assert not await verify_cdna_owner(interaction, 9)
    interaction.client.application_info.assert_not_awaited()
    interaction.user.id = 9
    interaction.client.application_info.return_value = SimpleNamespace(
        owner=SimpleNamespace(id=99),
    )
    assert not await verify_cdna_owner(interaction, 9)
    interaction.client.application_info.return_value.owner.id = 9
    assert await verify_cdna_owner(interaction, 9)


@pytest.mark.asyncio
async def test_single_click_research_uses_only_existing_exchange_ai(tmp_path):
    from tests.test_guided_home import _interaction
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "single_click.sqlite"), discord_token=None,
    ))
    home = bot._build_guided_home(42)
    home.navigate("crypto-desk")
    home.live_source = "draftkings"
    home.coin = "SOL"
    bot.cdna_personal.discover_coin = AsyncMock()
    panel = SimpleNamespace(
        asset="BTC", screen="opportunities", section="overview",
        market=object(), venues=("binance",),
        ai=SimpleNamespace(available=True, ask=AsyncMock(return_value=object())),
        _rebuild=lambda: None,
        render=AsyncMock(return_value=discord.Embed(title="AI result")),
    )
    home.research_factory = lambda _: panel
    try:
        interaction = _interaction(owner=42)
        await home.act(interaction, "dk-independent-ai")
        panel.ai.ask.assert_awaited_once_with(
            asset="SOL", venues=("binance",), requester_id=42,
        )
        bot.cdna_personal.discover_coin.assert_not_awaited()
        assert interaction.followup.send.await_count == 1
    finally:
        home.stop()
        await bot.cdna_personal.aclose()
        await bot.kalshi_browser.aclose()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
