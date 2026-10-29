"""Cross-venue Gemini caching, quota, and six-month history safety tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.gemini_research import GeminiResearch, ResearchUnavailable
from parlay_bot.perpetuals.history_inventory import history_inventory, six_months_prior
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def fixtures(market: PerpetualMarketStore, *, now: datetime, count: int = 289) -> None:
    period = timedelta(minutes=5)
    now = now.replace(second=0, microsecond=0)
    now = now.replace(minute=(now.minute // 5) * 5)
    for venue, contract, quote in (
        ("hyperliquid", "BTC", "USDC"),
        ("dydx", "BTC-USD", "USD"),
        ("bitget", "BTCUSDT", "USDT"),
    ):
        candles = []
        for i in range(count):
            opened = now - period * (count - i)
            base = 100 + i / 20
            candles.append(MarketCandle(
                venue, contract, quote, 5, opened,
                opened + period - timedelta(milliseconds=1), now,
                base, base + 3, base - 3, base + 1, 2.0, 15,
            ))
        assert market.ingest(MarketBatch((), tuple(candles), 5, now)).inserted_candles == count
        market.record_feed_health(
            venue=venue, asset="BTC", status="ok", attempted_at=now,
            new_candles=count, total_candles=count,
        )


@pytest.mark.asyncio
async def test_ai_evidence_consumes_all_healthy_venues_and_many_windows(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    clock = datetime.now(UTC).replace(second=0, microsecond=0)
    clock = clock.replace(minute=(clock.minute // 5) * 5)
    try:
        fixtures(market, now=clock)
        evidence, digest = build_evidence(
            market, asset="BTC",
            venues=("hyperliquid", "dydx", "bitget", "gate"),
            now=clock + timedelta(minutes=2),
        )
        assert len(evidence["sources"]) == 3
        assert {x["venue"] for x in evidence["sources"]} == {
            "hyperliquid", "dydx", "bitget",
        }
        assert evidence["unavailable_sources"] == [
            {"venue": "gate", "status": "never"}
        ]
        assert all(
            set(x["windows"]) == {"5m", "15m", "1h", "4h", "24h"}
            for x in evidence["sources"]
        )
        assert all(x["historical_windows"] == {} for x in evidence["sources"])
        assert evidence["horizon_minutes"] == 5
        assert len(digest) == 64
        assert digest == build_evidence(
            market, asset="BTC",
            venues=("hyperliquid", "dydx", "bitget", "gate"),
            now=clock + timedelta(minutes=3),
        )[1]
        with pytest.raises(InsufficientEvidence):
            build_evidence(market, asset="BTC",
                           venues=("hyperliquid", "dydx"), now=clock)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_database_cached_gemini_survives_restart_and_no_extra_requests(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    clock = datetime.now(UTC).replace(second=0, microsecond=0)
    clock = clock.replace(minute=clock.minute // 5 * 5)
    fixtures(market, now=clock)
    requests = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.headers["x-goog-api-key"] == "not-a-real-key"
        body = json.loads(request.content)
        prompt = body["contents"][0]["parts"][0]["text"]
        assert "hyperliquid" in prompt and "dydx" in prompt and "bitget" in prompt
        assert "24h" in prompt and "15m" in prompt
        assert "gate" in prompt  # Missing source is explicitly disclosed.
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{
            "text": json.dumps({
                "direction": "uncertain", "confidence": "low",
                "thesis": "Across-exchange momentum is mixed.",
                "risk": "No calibrated edge and no six-month data.",
            })
        }]}}]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    try:
        first = GeminiResearch(
            market, api_key="not-a-real-key", client=raw,
            daily_max=1, cooldown_seconds=900,
        )
        result = await first.ask(
            asset="BTC", venues=("hyperliquid", "dydx", "bitget", "gate"),
        )
        assert not result.from_cache
        assert result.source_count == 3
        second = GeminiResearch(
            market, api_key="not-a-real-key", client=raw,
            daily_max=1, cooldown_seconds=900,
        )
        cached = await second.ask(
            asset="BTC", venues=("hyperliquid", "dydx", "bitget", "gate"),
        )
        assert cached.from_cache and cached.evidence_sha256 == result.evidence_sha256
        assert len(requests) == 1
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM perp_ai_research").fetchone()["n"] == 1
            assert conn.execute("SELECT used FROM perp_ai_quota").fetchone()["used"] == 1
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_persisted_quota_rejects_requests_after_limit_and_honors_cooldown(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    # Daily budget resets at midnight UTC; this test exercises SAME-DAY caps.
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    try:
        assert market.reserve_ai_request(
            model="gemini-test", now=now, daily_max=2, cooldown_seconds=900,
        )
        assert not market.reserve_ai_request(
            model="gemini-test", now=now + timedelta(seconds=2),
            daily_max=2, cooldown_seconds=900,
        )
        assert market.reserve_ai_request(
            model="gemini-test", now=now + timedelta(minutes=16),
            daily_max=2, cooldown_seconds=900,
        )
        assert not market.reserve_ai_request(
            model="gemini-test", now=now + timedelta(minutes=32),
            daily_max=2, cooldown_seconds=900,
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ai_429_pauses_requests_across_service_instances(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    now = now.replace(minute=now.minute // 5 * 5)
    fixtures(market, now=now)
    calls = []
    def blocked(req):
        calls.append(req)
        return httpx.Response(429)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    try:
        service = GeminiResearch(market, api_key="fake", client=raw, cooldown_seconds=60)
        with pytest.raises(ResearchUnavailable, match="HTTP 429"):
            await service.ask(asset="BTC", venues=("hyperliquid", "dydx", "bitget"))
        another = GeminiResearch(market, api_key="fake", client=raw, cooldown_seconds=60)
        with pytest.raises(ResearchUnavailable, match="temporarily paused"):
            await another.ask(asset="BTC", venues=("hyperliquid", "dydx", "bitget"))
        assert len(calls) == 1
    finally:
        await raw.aclose()
        store.close()


def test_history_is_six_calendar_months_and_never_faked(tmp_path):
    assert six_months_prior(
        datetime(2026, 8, 31, tzinfo=UTC)
    ).date().isoformat() == "2026-02-28"
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    try:
        status = history_inventory(
            market, asset="BTC", venues=("hyperliquid", "dydx"),
            now=datetime(2026, 10, 28, tzinfo=UTC),
        )
        assert status["saved"] == 0
        assert status["expected"] > 100_000
        assert status["coverage_pct_ceiling"] == 0
        assert status["gap_audit"] == "not_yet_performed"
    finally:
        store.close()


def test_research_dashboard_does_not_claim_active_math_or_unconfigured_ai():
    view = ResearchDashboard(owner_id=15, market=None, ai=None, venues=())
    assert {x.custom_id for x in view.children} == {
        "rival:research:math", "rival:research:ai", "rival:research:asset",
        "rival:research:refresh", "rival:research:accuracy",
        "rival:research:opportunities",
        "rival:research:section",
    }
