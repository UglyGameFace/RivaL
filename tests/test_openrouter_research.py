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
        model("openai/gpt-oss-120b:free"),
        model("cohere/north-mini-code:free",
              expiry=(now-timedelta(seconds=1)).isoformat()),
        model("cohere/north-mini-code:free", paid=True),
        model("cohere/north-mini-code:free", endpoint=False),
        model("openrouter/free"),
        model("openai/gpt-6"),
    ]}
    assert module.eligible_free_coding_models(data, now=now) == (
        "openai/gpt-oss-120b:free",
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
                model("openai/gpt-oss-120b:free"),
                model("cohere/north-mini-code:free"),
                model("cohere/north-mini-code:free", paid=True),
            ]})
        assert req.url.path == "/api/v1/chat/completions"
        body = __import__("json").loads(req.content)
        assert body["models"] == [
            "openai/gpt-oss-120b:free", "cohere/north-mini-code:free",
        ]
        assert "Chainlink" in body["messages"][0]["content"]
        assert body["messages"][1]["content"] == (
            '{"sources":[{"venue":"hyperliquid"},'
            '{"venue":"binance"},{"venue":"okx"}]}'
        )
        return httpx.Response(200, json={"model": body["models"][0],
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
        assert models[0] == "openai/gpt-oss-120b:free"
        result = await reviewer.ask(asset="BTC", venues=("hyperliquid", "okx"))
        assert result.model == "openai/gpt-oss-120b:free"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_slug", ["selected-contract", "unrelated-contract"])
async def test_contract_review_uses_selected_target_and_rejects_foreign_reply(returned_slug):
    import json

    market = FakeMarket()
    payloads = []

    def respond(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [model("openai/gpt-oss-120b:free")]})
        body = json.loads(req.content)
        payloads.append(body)
        assert body["model"] == "openai/gpt-oss-120b:free"
        assert json.loads(body["messages"][1]["content"])["target"] == "polymarket"
        assert "exchange candles" not in body["messages"][1]["content"]
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {
            "content": json.dumps({"slug": returned_slug, "direction": "down", "confidence": "low",
                                   "thesis": "Selected-contract research", "risk": "Unvalidated"})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reviewer = module.OpenRouterResearch(market, api_key="test-secret", client=client)
        if returned_slug == "selected-contract":
            result = await reviewer.review_contract({"target": "polymarket", "slug": "selected-contract"})
            assert result["direction"] == "down"
        else:
            with pytest.raises(ResearchUnavailable, match="did not match"):
                await reviewer.review_contract({"target": "polymarket", "slug": "selected-contract"})
    assert market.calls == 1 and len(payloads) == 1
    assert market.saved == []  # never write contract calls to the perp-candle ledger


@pytest.mark.asyncio
async def test_contract_and_exchange_reviews_share_one_persisted_quota(tmp_path, monkeypatch):
    import json

    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import SQLiteHotStore

    hot = SQLiteHotStore(tmp_path / "one-budget.sqlite")
    market = PerpetualMarketStore(hot)
    completions = []

    def respond(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [model("openai/gpt-oss-120b:free")]})
        completions.append(req)
        return httpx.Response(200, json={"model": "openai/gpt-oss-120b:free", "choices": [{"message": {
            "content": json.dumps({"slug": "one-market", "direction": "up", "confidence": "low",
                                   "thesis": "Contract research", "risk": "Unvalidated"})}}]})

    monkeypatch.setattr(module, "build_evidence", lambda *a, **kw: ({"sources": [1, 2, 3]}, "x" * 64))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            one = module.OpenRouterResearch(market, api_key="test-secret", client=client)
            await one.review_contract({"target": "polymarket", "slug": "one-market"})
            restarted = module.OpenRouterResearch(market, api_key="test-secret", client=client)
            with pytest.raises(ResearchUnavailable, match="budget/cooldown"):
                await restarted.ask(asset="BTC", venues=("a", "b", "c"))
        assert len(completions) == 1
        with hot.connect() as conn:
            rows = conn.execute("SELECT model,used FROM perp_ai_quota").fetchall()
        assert len(rows) == 1 and rows[0]["used"] == 1
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_contract_quota_warning_is_local_and_names_exact_hold(monkeypatch):
    market = FakeMarket()
    market.reserve_ai_request = lambda **_kw: False
    market.ai_request_status = lambda **kw: {
        "model": kw["model"], "ready": False,
        "requests_today": 18, "rival_daily_limit": 18,
        "reasons": ("daily_budget_exhausted",),
        "retry_at": datetime(2026, 11, 1, tzinfo=UTC),
    }
    def respond(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [model("openai/gpt-oss-120b:free")]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reviewer = module.OpenRouterResearch(market, api_key="test", client=client)
        with pytest.raises(ResearchUnavailable) as err:
            await reviewer.review_contract({
                "target":"polymarket", "slug":"selected-contract",
            })
    message = str(err.value)
    assert "RivaL daily AI budget exhausted" in message
    assert "18/18" in message
    assert "not proof of OpenRouter account quota exhaustion" in message
    assert "<t:" in message


@pytest.mark.asyncio
async def test_upstream_503_pauses_free_requests_instead_of_hammering_provider(monkeypatch):
    market = FakeMarket()
    monkeypatch.setattr(
        module, "build_evidence",
        lambda *_args, **_kwargs: ({"sources": [1, 2, 3]}, "verified-digest"),
    )
    requests = []
    def respond(req):
        requests.append(req.url.path)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [model("openai/gpt-oss-120b:free")]})
        return httpx.Response(503, json={"error": {"message": "upstream unavailable"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reviewer = module.OpenRouterResearch(market, api_key="test-secret", client=client)
        with pytest.raises(ResearchUnavailable, match="HTTP 503"):
            await reviewer.ask(asset="BTC", venues=("a", "b", "c"))
    assert requests == ["/api/v1/models", "/api/v1/chat/completions"]
    assert market.calls == 1
    assert market.pauses
    assert market.pauses[0]["seconds"] == 1800
    assert market.saved == []


@pytest.mark.asyncio
async def test_fallback_only_uses_two_verified_free_models_and_records_served_model(monkeypatch):
    import json
    market = FakeMarket()
    monkeypatch.setattr(
        module, "build_evidence",
        lambda *_args, **_kwargs: ({"sources": [1, 2, 3]}, "verified-digest"),
    )
    sent = []
    def respond(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                model("openai/gpt-oss-120b:free"),
                model("poolside/laguna-s-2.1:free"),
                model("nvidia/nemotron-3.5-lightning:free"),
                model("cohere/north-mini-code:free", paid=True),
            ]})
        body = json.loads(req.content)
        sent.append(body)
        assert "model" not in body
        assert body["models"] == [
            "openai/gpt-oss-120b:free", "poolside/laguna-s-2.1:free",
        ]
        assert all(name.endswith(":free") for name in body["models"])
        return httpx.Response(200, json={
            "model": "poolside/laguna-s-2.1:free",
            "choices": [{"message": {"content": json.dumps({
                "direction": "uncertain", "confidence": "low",
                "thesis": "Provider fallback. No edge proved.",
                "risk": "Loss possible.",
            })}}],
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reviewer = module.OpenRouterResearch(market, api_key="test", client=client)
        result = await reviewer.ask(asset="ETH", venues=("a", "b", "c"))
    assert result.model == "poolside/laguna-s-2.1:free"
    assert market.saved[0]["model"] == result.model
    assert market.calls == 1
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_reject_unexpected_paid_fallback_even_if_provider_claims_success(monkeypatch):
    import json
    market = FakeMarket()
    monkeypatch.setattr(
        module, "build_evidence",
        lambda *_args, **_kwargs: ({"sources": [1, 2, 3]}, "verified-digest"),
    )
    def respond(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                model("openai/gpt-oss-120b:free"),
                model("nvidia/nemotron-3.5-lightning:free"),
            ]})
        return httpx.Response(200, json={
            "model": "openai/gpt-6",
            "choices": [{"message": {"content": json.dumps({
                "direction": "up", "confidence": "high",
                "thesis": "This is not a permitted free model.",
                "risk": "Paid substitution rejected.",
            })}}],
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reviewer = module.OpenRouterResearch(market, api_key="test", client=client)
        with pytest.raises(ResearchUnavailable, match="validation"):
            await reviewer.ask(asset="ETH", venues=("a", "b", "c"))
    assert market.calls == 1
    assert market.saved == []


def test_failing_recursive_flint_is_not_a_routing_candidate():
    now = datetime(2026, 10, 30, 22, tzinfo=UTC)
    found = module.eligible_free_coding_models({"data": [
        model("recursive/flint-v1.0:free"),
        model("openai/gpt-oss-120b:free"),
        model("poolside/laguna-s-2.1:free", expiry=(now+timedelta(days=1)).isoformat()),
        model("nvidia/nemotron-3.5-lightning:free"),
    ]}, now=now)
    assert found == (
        "openai/gpt-oss-120b:free",
        "poolside/laguna-s-2.1:free",
        "nvidia/nemotron-3.5-lightning:free",
    )
    assert "recursive/flint-v1.0:free" not in found
