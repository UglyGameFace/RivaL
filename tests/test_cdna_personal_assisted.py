"""Owner-only CDNA public research, fully isolated from model/trading paths."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    with pytest.raises(CDNAUnavailable, match="source update"):
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
        assert "Find CDNA Contract" in labels
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
