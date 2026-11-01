"""Standalone Cloudflare AI routing cannot bypass existing RivaL research guards."""
from __future__ import annotations

import json

import httpx
import pytest

from parlay_bot.perpetuals import cloudflare_research as module
from parlay_bot.perpetuals import openrouter_research
from parlay_bot.perpetuals.gemini_research import ResearchUnavailable


class Market:
    def __init__(self):
        self.reserved = []
        self.saved = []
        self.pauses = []

    def cached_ai_research(self, **kw):
        return None

    def reserve_ai_request(self, **kw):
        self.reserved.append(kw)
        return True

    def save_ai_research(self, **kw):
        self.saved.append(kw)

    def pause_ai_requests(self, **kw):
        self.pauses.append(kw)

    def ai_request_status(self, **kw):
        return {"model": kw["model"], "requests_today": 0,
                "rival_daily_limit": kw["daily_max"], "ready": True,
                "reasons": [], "retry_at": None}


@pytest.mark.asyncio
async def test_cloudflare_pinned_worker_and_no_provider_fallback(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence",
                        lambda *_args, **_kw: (
                            {"asset": "BTC", "sources": [{"venue": "gate"}]}, "c" * 64))
    seen = []

    def reply(request):
        seen.append(request)
        assert request.url.host == "rival-ai-research.staytrippynlw.workers.dev"
        assert request.url.path == "/v1/research"
        assert request.headers["Authorization"] == "Bearer local-test-secret"
        assert json.loads(request.content)["asset"] == "BTC"
        return httpx.Response(200, json={
            "model": module._MODEL, "direction": "uncertain",
            "confidence": "low", "thesis": "No validated edge",
            "risk": "No contemporaneous cost evidence",
            "uncalibrated": True, "informational_only": True,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        reviewer = module.CloudflareResearch(
            market, api_key="local-test-secret", client=client)
        result = await reviewer.ask(
            asset="BTC", venues=("gate",), requester_id=12345)
        assert result.model == module._MODEL
        assert result.direction == "uncertain"
        assert await reviewer.list_advertised_models() == (module._MODEL,)
        assert (await reviewer.request_status())["model"] == "rival-cloudflare-research"
    assert len(seen) == 1
    assert len(market.reserved) == 1
    assert market.reserved[0]["daily_max"] <= 18
    assert market.reserved[0]["requester_id"] == 12345
    assert market.saved[0]["model"] == module._MODEL


@pytest.mark.asyncio
async def test_cloudflare_daily_cap_error_pauses_without_fallback(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence",
                        lambda *_args, **_kw: (
                            {"asset": "SOL", "sources": [{"venue": "okx"}]}, "d" * 64))
    calls = []

    def reply(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": "daily_quota_exhausted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        reviewer = module.CloudflareResearch(
            market, api_key="local-test-secret", client=client)
        with pytest.raises(ResearchUnavailable, match="HTTP 429"):
            await reviewer.ask(asset="SOL", venues=("okx",))
    assert calls == ["/v1/research"]
    assert len(market.pauses) == 1
    assert market.pauses[0]["seconds"] == 3600
    assert market.saved == []


@pytest.mark.asyncio
async def test_cloudflare_rejects_untrusted_model_output(monkeypatch):
    market = Market()
    monkeypatch.setattr(openrouter_research, "build_evidence",
                        lambda *_args, **_kw: (
                            {"asset": "ETH", "sources": [{"venue": "bitget"}]}, "e" * 64))

    def reply(_request):
        return httpx.Response(200, json={
            "model": "paid-model", "direction": "up", "confidence": "high",
            "thesis": "Buy", "risk": "None",
            "uncalibrated": True, "informational_only": True,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        reviewer = module.CloudflareResearch(
            market, api_key="local-test-secret", client=client)
        with pytest.raises(ResearchUnavailable, match="invalid"):
            await reviewer.ask(asset="ETH", venues=("bitget",))
    assert market.saved == []


@pytest.mark.asyncio
async def test_cloudflare_is_explicit_and_contract_review_is_disabled():
    market = Market()
    reviewer = module.CloudflareResearch(market, api_key=None)
    assert not reviewer.available
    with pytest.raises(ResearchUnavailable, match="token"):
        await reviewer.list_advertised_models()
    with pytest.raises(ResearchUnavailable, match="not enabled"):
        await reviewer.review_contract({"target": "polymarket", "slug": "x"})
    await reviewer.aclose()
    with pytest.raises(ValueError, match="<=18"):
        module.CloudflareResearch(market, api_key="test", daily_max=25)
