"""Mock-transport coverage: the OpenRouter coding reviewer must NEVER bill."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals import openrouter_research as module
from parlay_bot.perpetuals.gemini_research import ResearchUnavailable


def model(name, *, paid=False, expiry=None, endpoint=True):
    return {
        "id": name,
        "pricing": {
            "prompt": "0.00001" if paid else "0",
            "completion": "0", "request": "0",
        },
        "architecture": {
            "input_modalities": ["text"], "output_modalities": ["text"],
        },
        "links": {
            "details": "/api/v1/models/cohere/north-mini-code/endpoints",
        } if endpoint else {},
        "expiration_date": expiry,
    }


def test_catalog_strictly_excludes_paid_expired_and_non_code_models():
    now = datetime(2026, 10, 30, 20, tzinfo=UTC)
    data = {"data": [
        model("recursive/flint-v1.0:free"),
        model("cohere/north-mini-code:free",
              expiry=(now-timedelta(seconds=1)).isoformat()),
        model("cohere/north-mini-code:free", paid=True),
        model("cohere/north-mini-code:free", endpoint=False),
        model("openrouter/free"),
        model("openai/gpt-6"),
    ]}
    assert module.eligible_free_coding_models(data, now=now) == (
        "recursive/flint-v1.0:free",
    )
    assert module.eligible_free_coding_models({"data": [
        model("cohere/north-mini-code:free", paid=True)
    ]}, now=now) == ()
    with pytest.raises(ResearchUnavailable):
        module.eligible_free_coding_models({"error": "permission"}, now=now)


class FakeMarket:
    def __init__(self):
        self.calls = 0
        self.saved = []
        self.pauses = []

    def cached_ai_research(self, **_kw):
        return None

    def reserve_ai_request(self, **kw):
        self.calls += 1
        assert kw["model"] == "rival-openrouter-free-coding"
        assert kw["daily_max"] <= 18
        return True

    def ai_request_status(self, **kw):
        return {
            "model": kw["model"], "ready": True,
            "requests_today": self.calls,
            "rival_daily_limit": kw["daily_max"],
            "reasons": [], "retry_at": None,
        }

    def pause_ai_requests(self, **kw):
        self.pauses.append(kw)

    def save_ai_research(self, **kw):
        self.saved.append(kw)


@pytest.mark.asyncio
async def test_free_only_coding_reviewer_uses_verified_evidence_and_budget(monkeypatch):
    market = FakeMarket()
    evidence = {"sources": [
        {"venue": "hyperliquid"}, {"venue": "binance"}, {"venue": "okx"},
    ]}
    monkeypatch.setattr(
        module, "build_evidence",
        lambda *args, **kw: (evidence, "sha-of-verified-input"),
    )
    observed = []

    def respond(req: httpx.Request):
        observed.append(req)
        assert req.headers["authorization"] == "Bearer test-secret"
        if req.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [
                model("recursive/flint-v1.0:free"),
                model("cohere/north-mini-code:free"),
                model("cohere/north-mini-code:free", paid=True),
            ]})
        assert req.url.path == "/api/v1/chat/completions"
        body = __import__("json").loads(req.content)
        assert body["model"] == "recursive/flint-v1.0:free"
        assert "Chainlink" in body["messages"][0]["content"]
        assert body["messages"][1]["content"] == (
            '{"sources":[{"venue":"hyperliquid"},'
            '{"venue":"binance"},{"venue":"okx"}]}'
        )
        return httpx.Response(200, json={"model": body["model"],
            "choices": [{"message": {"content": __import__("json").dumps({
                "direction": "uncertain", "confidence": "low",
                "thesis": "No demonstrated edge.", "risk": "Loss possible.",
            })}}],
        })

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as client:
        reviewer = module.OpenRouterResearch(
            market, api_key="test-secret", client=client,
        )
        models = await reviewer.list_advertised_models()
        assert models[0] == "recursive/flint-v1.0:free"
        result = await reviewer.ask(asset="BTC", venues=("hyperliquid", "okx"))
        assert result.model == "recursive/flint-v1.0:free"
        assert result.direction == "uncertain"
        assert result.evidence_sha256 == "sha-of-verified-input"
        assert len(market.saved) == 1
        assert market.saved[0]["model"] == result.model
        assert market.calls == 1
        assert len(observed) == 2
        status = await reviewer.request_status()
        assert status["requests_today"] == 1
        assert len(observed) == 2  # status is persisted only, no new HTTP


@pytest.mark.asyncio
async def test_paid_catalog_does_not_spend_a_model_request(monkeypatch):
    market = FakeMarket()
    monkeypatch.setattr(
        module, "build_evidence",
        lambda *_a, **_kw: ({"sources": [1, 2, 3]}, "digest"),
    )

    def respond(req):
        assert req.url.path == "/api/v1/models"
        return httpx.Response(200, json={"data": [
            model("cohere/north-mini-code:free", paid=True),
            model("deepseek/deepseek-v4"),
        ]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as client:
        reviewer = module.OpenRouterResearch(
            market, api_key="test-secret", client=client,
        )
        with pytest.raises(ResearchUnavailable, match="No curated free"):
            await reviewer.ask(asset="SOL", venues=("a", "b", "c"))
        assert market.calls == 0
        assert market.saved == []


@pytest.mark.asyncio
async def test_openrouter_payment_and_quota_errors_pause_but_never_fallback(
    monkeypatch,
):
    market = FakeMarket()
    monkeypatch.setattr(
        module, "build_evidence",
        lambda *_a, **_kw: ({"sources": [1, 2, 3]}, "digest"),
    )
    seen = []

    def respond(req):
        seen.append(req.url.path)
        if req.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [
                model("cohere/north-mini-code:free"),
            ]})
        return httpx.Response(402, json={"error": "payment required"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as client:
        reviewer = module.OpenRouterResearch(
            market, api_key="test-secret", client=client,
        )
        with pytest.raises(ResearchUnavailable, match="HTTP 402"):
            await reviewer.ask(asset="ETH", venues=("a", "b", "c"))
        assert seen == ["/api/v1/models", "/api/v1/chat/completions"]
        assert market.calls == 1
        assert len(market.pauses) == 1
        assert market.saved == []


def test_openrouter_budget_cannot_exceed_free_tier():
    with pytest.raises(ValueError, match="<=18"):
        module.OpenRouterResearch(FakeMarket(), api_key="x", daily_max=50)
