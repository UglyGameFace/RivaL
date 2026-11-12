"""Private CPU inference is authenticated, serialized, quota-capped and cached."""
from __future__ import annotations

import json

import httpx
import pytest

from parlay_bot.perpetuals import local_first_research as module
from parlay_bot.perpetuals.ai_types import ResearchUnavailable


class Market:
    def __init__(self):
        self.quota = []
        self.status_calls = []
        self.saved = []
        self.pauses = []

    def cached_ai_research(self, **_kw):
        return None

    def ai_request_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {
            "model": module.LOCAL_MODEL,
            "ready": True,
            "reasons": (),
            "retry_at": None,
            "requests_today": len(self.quota),
            "rival_daily_limit": kwargs.get("daily_max", 18),
        }

    def reserve_ai_request(self, **kwargs):
        self.quota.append(kwargs)
        return True

    def save_ai_research(self, **kwargs):
        self.saved.append(kwargs)

    def pause_ai_requests(self, **kwargs):
        self.pauses.append(kwargs)


def market_evidence(_market, *, asset, venues, horizon_minutes=5):
    return ({"asset": asset, "analysis": "unvalidated_research",
             "horizon_minutes": horizon_minutes,
             "sources": [{"venue": v} for v in venues]}, "a" * 64)


def success(model):
    return {
        "model": model, "direction": "uncertain", "confidence": "low",
        "thesis": "Insufficient forward evidence", "risk": "No matched quotes",
        "uncalibrated": True, "informational_only": True,
    }


@pytest.mark.asyncio
async def test_local_request_reserves_quota(monkeypatch):
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)
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
    # One reservation per LLM call, with the requester forwarded for the
    # per-requester cap (batch 2 §3.4).
    assert len(market.quota) == 1
    reserved = market.quota[0]
    assert reserved["model"] == module.LOCAL_MODEL
    assert reserved["daily_max"] == 0
    assert reserved["cooldown_seconds"] == 60
    assert reserved["requester_id"] == 49
    assert market.saved[0]["model"] == module.LOCAL_MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422, 429])
async def test_local_client_or_busy_failure_never_spends_backup(monkeypatch, status):
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(status, json={"error": "not_authorized_or_busy"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key",             client=client)
        with pytest.raises(ResearchUnavailable, match="Local AI rejected"):
            await ai.ask(asset="SOL", venues=("okx",))
    assert seen == ["rival-llm"]
    assert market.saved == []
    assert len(market.quota) == 1  # attempted call reserved; no backup spent


@pytest.mark.asyncio
async def test_untrusted_invalid_local_response_does_not_fallback(monkeypatch):
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        result = success(module.LOCAL_MODEL)
        result["informational_only"] = False
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key",             client=client)
        with pytest.raises(ResearchUnavailable, match="invalid research"):
            await ai.ask(asset="BTC", venues=("gate",))
    assert seen == ["rival-llm"]
    assert not market.saved
    assert len(market.quota) == 1


@pytest.mark.asyncio
async def test_local_failure_without_backup_yields_honest_data_only_reply(monkeypatch):
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)

    def handler(request):
        return httpx.Response(503, json={"error": "local_down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        opinion = await ai.ask(asset="BTC", venues=("gate",))
        assert opinion.model == module.EVIDENCE_MODEL
        assert opinion.direction == "uncertain"
        assert opinion.confidence == "low"
    assert len(market.quota) == 1  # one attempted call; gateway down, not unmetered
    assert market.saved[0]["model"] == module.EVIDENCE_MODEL
    assert market.pauses == []  # a single failure does not quarantine


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


@pytest.mark.asyncio
async def test_discloud_dns_failure_recovers_via_authenticated_discovery(monkeypatch):
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    calls = []
    discovered = []

    def lookup(token):
        discovered.append(token)
        return "172.4.72.19"

    monkeypatch.setattr(module, "discover_private_llm", lookup)

    def handler(request):
        calls.append(str(request.url.host))
        if request.url.host == "rival-llm":
            raise httpx.ConnectError("name resolution failed", request=request)
        assert request.url.host == "172.4.72.19"
        assert request.headers["Authorization"] == "Bearer " + "k" * 40
        return httpx.Response(200, json=success(module.LOCAL_MODEL))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="k" * 40, client=client)
        first = await ai.ask(asset="BTC", venues=("okx",))
        assert first.model == module.LOCAL_MODEL
        second = await ai.ask(asset="ETH", venues=("gate",))
        assert second.model == module.LOCAL_MODEL
    assert calls == ["rival-llm", "172.4.72.19", "172.4.72.19"]
    assert discovered == ["k" * 40]
    assert len(market.quota) == 2


@pytest.mark.asyncio
async def test_client_rechecks_direction_if_gateway_returns_unsupported_up(monkeypatch):
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)

    def handler(request):
        response = success(module.LOCAL_MODEL)
        response.update(direction="up", confidence="high",
                        thesis="Guaranteed next BTC candle up", risk="No risk")
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reviewer = module.LocalFirstResearch(
            market, local_key="x" * 48, client=http)
        report = await reviewer.ask(asset="BTC", venues=("okx",))
        assert report.direction == "uncertain"
        assert report.confidence == "low"
        assert "Guaranteed" not in report.thesis
    assert market.saved[0]["direction"] == "uncertain"
    assert len(market.quota) == 1

@pytest.mark.asyncio
async def test_data_only_research_uses_own_persisted_cache_without_extra_quota(monkeypatch):
    class Persisted(Market):
        def cached_ai_research(self, *, model, evidence_sha256, **_args):
            for row in self.saved:
                if row["model"] == model and row["evidence_sha256"] == evidence_sha256:
                    return {**row, "issued_at": row["issued_at"].isoformat()}
            return None

    market = Persisted()
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(503, json={"error": "model_timeout"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        first = await ai.ask(asset="BTC", venues=("gate", "okx", "bitget"))
        second = await ai.ask(asset="BTC", venues=("gate", "okx", "bitget"))
        assert first.model == second.model == module.EVIDENCE_MODEL
        assert second.from_cache
        assert first.direction == second.direction == "uncertain"
    assert len(seen) == 1
    # First ask reserves once; the exact-evidence cache hit costs nothing.
    assert len(market.quota) == 1
    assert len(market.saved) == 1


@pytest.mark.asyncio
async def test_local_hypothesis_reserves_quota(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    market = Market()
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "rival-llm"
        assert request.url.path == "/v1/hypothesis"
        assert request.headers["Authorization"] == "Bearer local-key"
        assert json.loads(request.content)["asset"] == "XRP"
        return httpx.Response(200, json={
            "model": module.LOCAL_MODEL,
            "hypothesis": "volume_only",
            "rationale": "Compare native volume features",
            "falsification": "Reject worse validation Brier",
            "research_only": True, "retrospective_screen": "not_tested",
            "live_model_promoted": False, "prediction": None,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key",             client=client,
        )
        proposed = await ai.propose_feature_hypothesis(
            asset="XRP", venues=("okx", "gate"),
        )
        assert proposed["model"] == module.LOCAL_MODEL
        assert proposed["hypothesis"] == "volume_only"
        assert proposed["evidence_sha256"] == "a" * 64
        assert proposed["venues"] == ["gate", "okx"]
        assert proposed["research_only"]
    assert len(seen) == 1
    assert len(market.quota) == 1
    assert not market.saved


@pytest.mark.asyncio
async def test_unavailable_hypothesis_never_invents_proposals(
    monkeypatch,
):
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    market = Market()
    requested = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(503, json={"error": "offline"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(
            market, local_key="local-key",             client=client,
        )
        with pytest.raises(ResearchUnavailable, match="hypothesis endpoint unavailable"):
            await ai.propose_feature_hypothesis(
                asset="BTC", venues=("okx",),
            )
    assert requested == ["rival-llm"]
    assert len(market.quota) == 1  # attempted call reserved; no proposal invented
    assert market.saved == []


@pytest.mark.asyncio
async def test_private_intelligence_reports_live_budget_and_no_external_catalog():
    """The private CPU dashboard reports usage and its real capacity policy."""
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard
    ai = module.LocalFirstResearch(Market(), local_key="owner-key")
    try:
        status = await ai.request_status()
        assert "unmetered_local" not in status
        assert status["ready"] is True
        assert status["requests_today"] == 0
        assert status["rival_daily_limit"] == 0
        assert status["reasons"] == ()
        assert await ai.list_advertised_models() == (module.LOCAL_MODEL,)
        dashboard = ResearchDashboard(owner_id=42, market=None, ai=ai, venues=())
        dashboard.screen = "ai"
        dashboard.section = "limits"
        dashboard._rebuild()
        embed = await dashboard.render()
        fields = {field.name: field.value for field in embed.fields}
        assert "Private LFM3 Intelligence" in fields
        assert "No paid tokens" in fields["Private LFM3 Intelligence"]
        assert "0 requests today; no daily cap" in fields["Private LFM3 Intelligence"]
        assert "60 s minimum spacing" in fields["Private LFM3 Intelligence"]
        assert "OpenRouter" not in str(fields) and "Cloudflare" not in str(fields)
    finally:
        await ai.aclose()


@pytest.mark.asyncio
async def test_twenty_five_local_requests_each_reserve_quota(monkeypatch):
    market = Market()
    counter = 0
    sent = []

    def changing_evidence(_market, *, asset, venues, horizon_minutes=5):
        nonlocal counter
        counter += 1
        return ({"asset": asset, "analysis": "unvalidated_research",
                 "horizon_minutes": horizon_minutes,
                 "sources": [{"venue": v} for v in venues]}, f"{counter:064x}")

    def handler(request):
        sent.append(request.url.host)
        return httpx.Response(200, json=success(module.LOCAL_MODEL))

    monkeypatch.setattr(module, "build_evidence", changing_evidence)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reviewer = module.LocalFirstResearch(market, local_key="owner", client=client)
        for _ in range(25):
            await reviewer.ask(asset="BTC", venues=("okx",))
    assert sent == ["rival-llm"] * 25
    assert len(market.saved) == 25
    # Batch 2 §3.4: every LLM call reserves quota — 25 calls, 25 reservations.
    # (The fake market never refuses, so no cap is hit here; the cap itself
    # is covered against the real store in test_batch2_budget_rewire.py.)
    assert len(market.quota) == 25


def _hypothesis_reply():
    return {
        "model": module.LOCAL_MODEL,
        "hypothesis": "volume_only",
        "rationale": "Compare native volume features",
        "falsification": "Reject worse validation Brier",
        "research_only": True, "retrospective_screen": "not_tested",
        "live_model_promoted": False, "prediction": None,
    }


def test_failure_detail_keeps_only_allowlisted_codes():
    response = httpx.Response(503, json={
        "error": "inference_failed", "failure_kind": "token_limit",
    })
    assert module.failure_detail(response) == (
        "status=503 error=inference_failed failure_kind=token_limit"
    )
    hostile = httpx.Response(503, json={
        "error": "Bearer sk-secret leaked", "failure_kind": ["x"],
    })
    assert module.failure_detail(hostile) == "status=503 error=none failure_kind=none"
    assert module.failure_detail(httpx.Response(502, text="<html>")) == (
        "status=502 error=none failure_kind=none"
    )


@pytest.mark.asyncio
async def test_hypothesis_logs_gateway_failure_kind(monkeypatch, caplog):
    monkeypatch.setattr(module, "build_evidence", market_evidence)

    def handler(request):
        return httpx.Response(503, json={
            "error": "inference_failed", "failure_kind": "token_limit",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(Market(), local_key="local-key", client=client)
        with (
            caplog.at_level("WARNING", logger=module.__name__),
            pytest.raises(ResearchUnavailable) as raised,
        ):
            await ai.propose_feature_hypothesis(asset="BTC", venues=("okx",))
    # token_limit repeats deterministically: reported, never retried.
    assert not isinstance(raised.value, module.LocalBusy)
    assert "failure_kind=token_limit" in caplog.text
    assert "failure_kind=token_limit" in str(raised.value)


@pytest.mark.asyncio
async def test_hypothesis_retries_busy_gateway_then_succeeds(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    monkeypatch.setattr(module, "BUSY_RETRY_DELAYS", (0.0, 0.0, 0.0))
    replies = [
        httpx.Response(429, json={"error": "inference_busy"}),
        httpx.Response(503, json={"error": "inference_failed",
                                  "failure_kind": "backend_timeout"}),
        httpx.Response(200, json=_hypothesis_reply()),
    ]
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return replies[len(seen) - 1]

    market = Market()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        proposed = await ai.propose_feature_hypothesis(asset="XRP", venues=("okx",))
        # The lock is released between attempts and after success.
        assert not ai._lock.locked()
    assert proposed["hypothesis"] == "volume_only"
    assert seen == ["/v1/hypothesis"] * 3
    # Busy retries reuse the one reservation; they do not spend quota twice.
    assert len(market.quota) == 1


@pytest.mark.asyncio
async def test_hypothesis_busy_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    monkeypatch.setattr(module, "BUSY_RETRY_DELAYS", (0.0, 0.0, 0.0))
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(429, json={"error": "inference_busy"})

    market = Market()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        with pytest.raises(module.LocalBusy):
            await ai.propose_feature_hypothesis(asset="BTC", venues=("okx",))
    assert len(seen) == 4  # first try plus three retries
    assert len(market.quota) == 1  # one reservation for the whole call


@pytest.mark.asyncio
async def test_interactive_ask_never_waits_but_background_ask_retries(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", market_evidence)
    monkeypatch.setattr(module, "BUSY_RETRY_DELAYS", (0.0, 0.0, 0.0))
    market = Market()
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if len(seen) in (1, 2):
            return httpx.Response(429, json={"error": "inference_busy"})
        return httpx.Response(200, json=success(module.LOCAL_MODEL))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        with pytest.raises(module.LocalBusy, match="Local AI rejected"):
            await ai.ask(asset="ETH", venues=("okx",))
        assert len(seen) == 1
        opinion = await ai.ask(asset="ETH", venues=("okx",), busy_retry=True)
    assert opinion.model == module.LOCAL_MODEL
    assert len(seen) == 3
    assert len(market.saved) == 1
    # Two ask calls, two reservations; the busy retry inside the second
    # call reuses its reservation instead of spending a second unit.
    assert len(market.quota) == 2


@pytest.mark.asyncio
async def test_explain_models_grounds_thesis_and_risk(monkeypatch):
    """P1-10 remainder: the bot re-enforces explain reviews, so an outdated
    gateway cannot store or display raw LLM prose."""
    market = Market()
    monkeypatch.setattr(module, "build_evidence", market_evidence)

    def handler(request):
        assert request.url.path == "/v1/explain"
        return httpx.Response(200, json={
            "model": module.LOCAL_MODEL, "mode": "explain",
            "stance_5m": "follow", "stance_15m": "abstain", "stance_60m": "abstain",
            "thesis": "All three exchanges lean up; buy the breakout.",
            "risk": "Small edge, but looks safe.",
            "informational_only": True, "uncalibrated": True,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ai = module.LocalFirstResearch(market, local_key="local-key", client=client)
        review = await ai.explain_models(
            asset="BTC", venues=("okx", "gate", "bitget"),
            calls={"5": {"live": {"p_up": 0.6, "venues": 3, "venues_up": 3}}},
        )
    assert review["stance_5m"] == "follow"
    assert review["stance_15m"] == "abstain"
    assert "direction" not in review
    assert "lean up" not in review["thesis"] and "breakout" not in review["thesis"]
    assert "looks safe" not in review["risk"]
    assert review["thesis"] == (
        "Insufficient fresh independent closed-candle evidence to "
        "support a market direction.")
    assert review["risk"].startswith("No verified future advantage")
    assert review["grounded_by"] == "deterministic_historical_evidence_policy"
    assert len(market.quota) == 1  # explain reserves quota before the POST
