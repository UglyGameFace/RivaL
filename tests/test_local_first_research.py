"""Local-first research uses the existing one-request accounting and safe fallback."""
from __future__ import annotations

import json

import httpx
import pytest

from parlay_bot.perpetuals import local_first_research as module
from parlay_bot.perpetuals import openrouter_research
from parlay_bot.perpetuals.gemini_research import ResearchUnavailable


class Market:
    def __init__(self):
        self.quota = []
        self.saved = []
        self.pauses = []

    def cached_ai_research(self, **_kw):
        return None

    def reserve_ai_request(self, **kwargs):
        self.quota.append(kwargs)
        return True

    def save_ai_research(self, **kwargs):
        self.saved.append(kwargs)

    def pause_ai_requests(self, **kwargs):
        self.pauses.append(kwargs)


def market_evidence(_market, *, asset, venues, horizon_minutes=5):
    return ({"asset": asset, "sources": [{"venue": v} for v in venues]}, "a" * 64)


def success(model):
    return {
        "model": model, "direction": "uncertain", "confidence": "low",
        "thesis": "Insufficient forward evidence", "risk": "No matched quotes",
        "uncalibrated": True, "informational_only": True,
    }


@pytest.mark.asyncio
async def test_local_only_one_request_one_quota(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence", market_evidence)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "rival-llm"
        assert request.url.path == "/v1/research"
        assert request.headers["Authorization"] == "Bearer local-key"
        assert json.loads(request.content)["asset"] == "ETH"
        return httpx.Response(200, json=success(module.LOCAL_MODEL))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        opinion = await ai.ask(asset="ETH", venues=("okx", "gate"), requester_id=49)
        assert opinion.model == module.LOCAL_MODEL
    assert len(seen) == 1
    assert len(market.quota) == 1
    assert market.quota[0]["requester_id"] == 49
    assert market.quota[0]["daily_max"] == 18
    assert market.saved[0]["model"] == module.LOCAL_MODEL


@pytest.mark.asyncio
async def test_cloudflare_is_backup_only_on_local_503(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence", market_evidence)
    requested = []

    def handler(request):
        requested.append((request.url.host, request.headers["Authorization"]))
        if request.url.host == "rival-llm":
            return httpx.Response(503, json={"error": "local_model_unavailable"})
        assert request.url.host == "rival-ai-research.staytrippynlw.workers.dev"
        return httpx.Response(200, json=success(module.CLOUD_MODEL))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key", cloudflare_key="backup-key",
            client=client)
        result = await ai.ask(asset="BTC", venues=("a", "b"), requester_id=123)
        assert result.model == module.CLOUD_MODEL
        assert await ai.list_advertised_models() == (
            module.LOCAL_MODEL, module.CLOUD_MODEL)
    assert requested == [
        ("rival-llm", "Bearer local-key"),
        ("rival-ai-research.staytrippynlw.workers.dev", "Bearer backup-key"),
    ]
    assert len(market.quota) == 1
    assert market.saved[0]["model"] == module.CLOUD_MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_local_client_or_busy_failure_never_spends_backup(monkeypatch, status):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence", market_evidence)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(status, json={"error": "not_authorized_or_busy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key", cloudflare_key="backup-key",
            client=client)
        with pytest.raises(ResearchUnavailable, match="Local AI rejected"):
            await ai.ask(asset="SOL", venues=("okx",))
    assert seen == ["rival-llm"]
    assert market.saved == []


@pytest.mark.asyncio
async def test_untrusted_invalid_local_response_does_not_fallback(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence", market_evidence)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        result = success(module.LOCAL_MODEL)
        result["informational_only"] = False
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key", cloudflare_key="backup-key",
            client=client)
        with pytest.raises(ResearchUnavailable, match="invalid research"):
            await ai.ask(asset="BTC", venues=("gate",))
    assert seen == ["rival-llm"]
    assert not market.saved


@pytest.mark.asyncio
async def test_local_failure_without_backup_circuit_breaks(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence", market_evidence)

    def handler(request):
        return httpx.Response(503, json={"error": "local_down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        with pytest.raises(ResearchUnavailable, match="no Cloudflare backup"):
            await ai.ask(asset="BTC", venues=("gate",))
    assert len(market.pauses) == 1
    assert market.pauses[0]["seconds"] == 1800


@pytest.mark.asyncio
async def test_no_contracts_or_missing_local_secret():
    market = Market()
    ai = module.LocalFirstResearch(market, local_key=None)
    assert not ai.available
    with pytest.raises(ResearchUnavailable, match="not configured"):
        await ai.list_advertised_models()
    with pytest.raises(ResearchUnavailable, match="oracle"):
        await ai.review_contract({"target": "polymarket", "slug": "x"})
    await ai.aclose()
