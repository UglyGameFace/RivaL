"""Batch 2 Phase 2 — budget re-wire (spec §3.4, D5).

The persisted quota tables (market_store.py:563-722) are now enforced on
every local LFM3 call: ask()/explain_models()/propose_feature_hypothesis()
reserve quota first, background loops run only with the operator opt-in,
and three backend failures in 1800 s quarantine the backend through
pause_ai_requests(). Table-level logic itself is covered by
test_gemini_quota_status.py, test_ai_user_fair_quota.py,
test_perpetuals_ai_postgres.py and test_perpetuals_ai_research.py; these
tests cover the wiring, not the tables.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient, llm_background_enabled
from parlay_bot.perpetuals import local_first_research as module
from parlay_bot.perpetuals.ai_types import ResearchUnavailable
from parlay_bot.perpetuals.local_first_research import LocalFirstResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class FakeClock(datetime):
    """Controllable UTC clock for the 60 s spacing rule (no real sleeps)."""

    _now: datetime = START

    @classmethod
    def now(cls, tz=None):
        current = cls._now
        return current if tz is None else current.astimezone(tz)

    @classmethod
    def reset(cls, when: datetime = START) -> None:
        cls._now = when

    @classmethod
    def advance(cls, **kwargs) -> None:
        cls._now = cls._now + timedelta(**kwargs)


def success(model):
    return {
        "model": model, "direction": "uncertain", "confidence": "low",
        "thesis": "Insufficient forward evidence", "risk": "No matched quotes",
        "uncalibrated": True, "informational_only": True,
    }


def hypothesis_reply():
    return {
        "model": module.LOCAL_MODEL,
        "hypothesis": "volume_only",
        "rationale": "Compare native volume features",
        "falsification": "Reject worse validation Brier",
        "research_only": True, "retrospective_screen": "not_tested",
        "live_model_promoted": False, "prediction": None,
    }


class ChangingEvidence:
    """Distinct evidence per call so no ask is served from cache."""

    def __init__(self):
        self.counter = 0

    def __call__(self, _market, *, asset, venues, horizon_minutes=5):
        self.counter += 1
        # save_ai_research requires >= 3 sources; use three fixed venues.
        return ({"asset": asset, "analysis": "unvalidated_research",
                 "horizon_minutes": horizon_minutes,
                 "sources": [{"venue": v} for v in ("okx", "gate", "bitget")]},
                f"{self.counter:064x}")


@pytest.fixture
def clock(monkeypatch):
    FakeClock.reset()
    monkeypatch.setattr(module, "datetime", FakeClock)
    return FakeClock


@pytest.fixture
def real_market(tmp_path):
    store = SQLiteHotStore(tmp_path / "budget.sqlite")
    market = PerpetualMarketStore(store)
    try:
        yield market
    finally:
        store.close()


def make_ai(market, handler, *, daily_max=18, monkeypatch=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LocalFirstResearch(
        market, local_key="local-key", client=client, daily_max=daily_max,
    )


async def close(ai):
    await ai.aclose()


# --- config defaults (D5) -------------------------------------------------

def test_budget_config_defaults_are_18_per_day_and_opt_in_off():
    settings = Settings(_env_file=None)
    assert settings.rival_llm_daily_max == 18
    assert settings.rival_llm_background_opt_in is False
    assert not llm_background_enabled(settings)
    opted_in = Settings(_env_file=None, rival_llm_background_opt_in=True)
    assert llm_background_enabled(opted_in)


def test_auto_ai_stays_false_in_bot():
    code = (ROOT / "src/parlay_bot/discord_app/bot.py").read_text()
    import re
    assert re.findall(r"auto_ai\s*=\s*\w+", code) == ["auto_ai=False"]


# --- ask() against the real store -----------------------------------------

@pytest.mark.asyncio
async def test_ask_per_requester_cap_fourth_refused(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    seen = []
    ai = make_ai(real_market, lambda r: (seen.append(r), httpx.Response(
        200, json=success(module.LOCAL_MODEL)))[1])
    try:
        for _ in range(3):
            opinion = await ai.ask(asset="BTC", venues=("okx",), requester_id=1234)
            assert opinion.model == module.LOCAL_MODEL
            clock.advance(seconds=61)
        with pytest.raises(ResearchUnavailable) as raised:
            await ai.ask(asset="BTC", venues=("okx",), requester_id=1234)
        assert "requester_budget_exhausted" in str(raised.value)
        assert "retry_at" in str(raised.value)
        assert len(seen) == 3  # refused call never ran inference
        status = real_market.ai_request_status(
            model=module.LOCAL_MODEL, now=FakeClock.now(UTC),
            daily_max=18, cooldown_seconds=60, requester_id=1234,
        )
        assert status["requests_today"] == 3
        assert status["requester_used_today"] == 3
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_ask_minimum_spacing_enforced(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    seen = []
    ai = make_ai(real_market, lambda r: (seen.append(r), httpx.Response(
        200, json=success(module.LOCAL_MODEL)))[1])
    try:
        await ai.ask(asset="BTC", venues=("okx",), requester_id=111)
        clock.advance(seconds=10)
        with pytest.raises(ResearchUnavailable) as raised:
            await ai.ask(asset="BTC", venues=("okx",), requester_id=222)
        assert "minimum_spacing" in str(raised.value)
        assert len(seen) == 1
        clock.advance(seconds=51)
        await ai.ask(asset="BTC", venues=("okx",), requester_id=222)
        assert len(seen) == 2
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_ask_daily_cap_18_enforced(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    seen = []
    ai = make_ai(real_market, lambda r: (seen.append(r), httpx.Response(
        200, json=success(module.LOCAL_MODEL)))[1], daily_max=3)
    try:
        for requester in (11, 22, 33):
            await ai.ask(asset="BTC", venues=("okx",), requester_id=requester)
            clock.advance(seconds=61)
        with pytest.raises(ResearchUnavailable) as raised:
            await ai.ask(asset="BTC", venues=("okx",), requester_id=44)
        assert "daily_budget_exhausted" in str(raised.value)
        assert "2026-11-07" in str(raised.value)  # retry at next UTC midnight
        assert len(seen) == 3
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_ask_cache_hit_costs_no_quota(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", lambda _m, **kw: (
        {"asset": "BTC", "analysis": "unvalidated_research",
         "horizon_minutes": 5,
         "sources": [{"venue": v} for v in ("okx", "gate", "bitget")]}, "b" * 64))
    seen = []
    ai = make_ai(real_market, lambda r: (seen.append(r), httpx.Response(
        200, json=success(module.LOCAL_MODEL)))[1])
    try:
        first = await ai.ask(asset="BTC", venues=("okx",))
        second = await ai.ask(asset="BTC", venues=("okx",))
        assert first.model == second.model == module.LOCAL_MODEL
        assert second.from_cache
        assert len(seen) == 1
        status = real_market.ai_request_status(
            model=module.LOCAL_MODEL, now=FakeClock.now(UTC),
            daily_max=18, cooldown_seconds=60,
        )
        assert status["requests_today"] == 1
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_ask_reports_live_quota_after_one_call(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    ai = make_ai(real_market, lambda r: httpx.Response(
        200, json=success(module.LOCAL_MODEL)))
    try:
        await ai.ask(asset="BTC", venues=("okx",))
        status = await ai.request_status()
        assert status["requests_today"] == 1
        assert status["rival_daily_limit"] == 18
        assert status["ready"] is False  # 60 s spacing still active
        assert status["reasons"] == ("minimum_spacing",)
        assert "unmetered_local" not in status
    finally:
        await close(ai)


# --- cap hit without a market: reason + retry_at, no inference -----------

class BlockedMarket:
    """ai_request_status refuses; reserve must never be reached."""

    def __init__(self, retry_at):
        self.retry_at = retry_at
        self.reserved = 0

    def cached_ai_research(self, **_kw):
        return None

    def ai_request_status(self, **_kw):
        return {
            "model": module.LOCAL_MODEL, "ready": False,
            "reasons": ("daily_budget_exhausted",),
            "retry_at": self.retry_at, "requests_today": 18,
            "rival_daily_limit": 18,
        }

    def reserve_ai_request(self, **_kw):
        self.reserved += 1
        return True

    def save_ai_research(self, **_kw):
        raise AssertionError("no research may be saved on a cap hit")

    def pause_ai_requests(self, **_kw):
        raise AssertionError("no pause expected here")


class ReadyMarket(BlockedMarket):
    def __init__(self):
        super().__init__(retry_at=None)
        self.status_calls = []

    def ai_request_status(self, **_kw):
        self.status_calls.append(_kw)
        return {
            "model": module.LOCAL_MODEL, "ready": True, "reasons": (),
            "retry_at": None, "requests_today": 0, "rival_daily_limit": 18,
        }


@pytest.mark.asyncio
async def test_ask_cap_hit_returns_reason_and_retry_at_without_inference(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    retry_at = datetime(2026, 11, 7, tzinfo=UTC)
    market = BlockedMarket(retry_at)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=success(module.LOCAL_MODEL))

    ai = make_ai(market, handler)
    try:
        with pytest.raises(ResearchUnavailable) as raised:
            await ai.ask(asset="BTC", venues=("okx",), requester_id=7)
        assert "daily_budget_exhausted" in str(raised.value)
        assert "2026-11-07" in str(raised.value)
        assert seen == []
        assert market.reserved == 0
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_explain_models_cap_hit_never_posts(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    market = BlockedMarket(datetime(2026, 11, 7, tzinfo=UTC))
    seen = []
    ai = make_ai(market, lambda r: (seen.append(r), httpx.Response(
        200, json={"model": module.LOCAL_MODEL}))[1])
    try:
        with pytest.raises(ResearchUnavailable, match="daily_budget_exhausted"):
            await ai.explain_models(
                asset="BTC", venues=("okx",),
                calls={"5": {"live": {"p_up": 0.6}}},
            )
        assert seen == []
        assert market.reserved == 0
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_explain_models_reserves_quota_before_post(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    market = ReadyMarket()
    seen = []
    ai = make_ai(market, lambda r: (seen.append(r), httpx.Response(200, json={
        "model": module.LOCAL_MODEL, "mode": "explain",
        "stance_5m": "follow", "stance_15m": "abstain", "stance_60m": "abstain",
        "thesis": "All three exchanges lean up; buy the breakout.",
        "risk": "Small edge, but looks safe.",
        "informational_only": True, "uncalibrated": True,
    }))[1])
    try:
        review = await ai.explain_models(
            asset="BTC", venues=("okx", "gate", "bitget"),
            calls={"5": {"live": {"p_up": 0.6, "venues": 3, "venues_up": 3}}},
        )
        assert review["stance_5m"] == "follow"
        assert len(seen) == 1
        assert market.reserved == 1
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_propose_cap_hit_never_posts_and_success_reserves_once(monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    blocked = BlockedMarket(datetime(2026, 11, 7, tzinfo=UTC))
    seen = []
    ai = make_ai(blocked, lambda r: (seen.append(r), httpx.Response(
        200, json=hypothesis_reply()))[1])
    try:
        with pytest.raises(ResearchUnavailable, match="daily_budget_exhausted"):
            await ai.propose_feature_hypothesis(asset="BTC", venues=("okx",))
        assert seen == []
        assert blocked.reserved == 0
    finally:
        await close(ai)

    ready = ReadyMarket()
    ai = make_ai(ready, lambda r: (seen.append(r), httpx.Response(
        200, json=hypothesis_reply()))[1])
    try:
        proposed = await ai.propose_feature_hypothesis(asset="BTC", venues=("okx",))
        assert proposed["hypothesis"] == "volume_only"
        assert len(seen) == 1
        assert ready.reserved == 1
    finally:
        await close(ai)


# --- circuit breaker ------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_pause_blocks_ask_and_expires(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    seen = []
    ai = make_ai(real_market, lambda r: (seen.append(r), httpx.Response(
        200, json=success(module.LOCAL_MODEL)))[1])
    try:
        await ai.pause_ai_requests(seconds=3600)
        with pytest.raises(ResearchUnavailable) as raised:
            await ai.ask(asset="BTC", venues=("okx",))
        assert "provider_error_pause" in str(raised.value)
        assert seen == []
        clock.advance(seconds=3600)
        await ai.ask(asset="BTC", venues=("okx",))
        assert len(seen) == 1
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_three_backend_failures_quarantine(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    seen = []
    ai = make_ai(real_market, lambda r: (seen.append(r), httpx.Response(
        503, json={"error": "local_down"}))[1])
    try:
        # Each 503 falls back to the deterministic evidence summary; the
        # call still counts as one backend failure.
        for _ in range(2):
            opinion = await ai.ask(asset="BTC", venues=("okx",))
            assert opinion.model == module.EVIDENCE_MODEL
            clock.advance(seconds=61)
        status = real_market.ai_request_status(
            model=module.LOCAL_MODEL, now=FakeClock.now(UTC),
            daily_max=18, cooldown_seconds=60,
        )
        assert "provider_error_pause" not in status["reasons"]
        opinion = await ai.ask(asset="BTC", venues=("okx",))
        assert opinion.model == module.EVIDENCE_MODEL
        status = real_market.ai_request_status(
            model=module.LOCAL_MODEL, now=FakeClock.now(UTC),
            daily_max=18, cooldown_seconds=60,
        )
        assert "provider_error_pause" in status["reasons"]
        assert status["retry_at"] == START + timedelta(seconds=61 * 2 + 3600)
        clock.advance(seconds=61)
        with pytest.raises(ResearchUnavailable, match="provider_error_pause"):
            await ai.ask(asset="BTC", venues=("okx",))
        assert len(seen) == 3  # quarantined call never reached the gateway
    finally:
        await close(ai)


@pytest.mark.asyncio
async def test_backend_success_resets_failure_count(real_market, clock, monkeypatch):
    monkeypatch.setattr(module, "build_evidence", ChangingEvidence())
    replies = [503, 503, 200, 503, 503]
    seen = []

    def handler(request):
        seen.append(request)
        code = replies[len(seen) - 1]
        if code == 200:
            return httpx.Response(200, json=success(module.LOCAL_MODEL))
        return httpx.Response(503, json={"error": "local_down"})

    ai = make_ai(real_market, handler)
    try:
        for _ in range(5):
            await ai.ask(asset="BTC", venues=("okx",))
            clock.advance(seconds=61)
        status = real_market.ai_request_status(
            model=module.LOCAL_MODEL, now=FakeClock.now(UTC),
            daily_max=18, cooldown_seconds=60,
        )
        assert "provider_error_pause" not in status["reasons"]
        assert len(seen) == 5
    finally:
        await close(ai)


# --- background loops: opt-in gate ----------------------------------------

def _bare_bot(*, opt_in: bool):
    bot = object.__new__(RivalDiscordClient)
    bot.settings = Settings(
        _env_file=None, rival_llm_background_opt_in=opt_in,
    )
    return bot


@pytest.mark.asyncio
async def test_background_loops_do_not_run_without_opt_in():
    bot = _bare_bot(opt_in=False)
    waited = []

    async def fake_wait():
        waited.append(True)

    bot.wait_until_ready = fake_wait
    await bot._hypothesis_loop()
    await bot._intelligence_loop()
    assert waited == []  # neither loop waited: both exited at the opt-in head


@pytest.mark.asyncio
async def test_background_loops_proceed_past_head_when_opted_in():
    bot = _bare_bot(opt_in=True)
    waited = []

    async def fake_wait():
        waited.append(True)
        raise asyncio.CancelledError

    bot.wait_until_ready = fake_wait
    with pytest.raises(asyncio.CancelledError):
        await bot._hypothesis_loop()
    with pytest.raises(asyncio.CancelledError):
        await bot._intelligence_loop()
    assert waited == [True, True]


def test_background_loop_wiring_requires_opt_in():
    """Task creation, both loop heads, and the watchdog all check the gate."""
    code = (ROOT / "src/parlay_bot/discord_app/bot.py").read_text()
    assert code.count("llm_background_enabled(self.settings)") >= 5
    assert "and llm_background_enabled(self.settings))" in code
    assert "and llm_background_enabled(self.settings)" in code


def test_user_initiated_paths_do_not_consult_opt_in():
    """Analyze / dk-independent-ai ask() directly; the opt-in gates only
    the automatic loops. The ask-level tests above prove those calls
    succeed with the default (opted-out) settings and still reserve quota."""
    for name in ("research_dashboard.py", "home.py"):
        code = (ROOT / "src/parlay_bot/discord_app" / name).read_text()
        assert "rival_llm_background_opt_in" not in code
    code = (ROOT / "src/parlay_bot/perpetuals/local_first_research.py").read_text()
    assert "llm_background_enabled" not in code
    assert "self.settings" not in code
