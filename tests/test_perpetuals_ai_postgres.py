"""Production Postgres compatibility for persistent Gemini response and quota caching."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore


def test_postgres_ai_cache_quota_and_circuit_breaker_are_restart_safe():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres integration service not configured")
    store = PostgresHotStore(dsn)
    market = PerpetualMarketStore(store)
    model = "rival-postgres-cache-ci"
    now = datetime(2026, 10, 28, 13, 0, tzinfo=UTC)
    try:
        market.initialize()
        assert market.reserve_ai_request(
            model=model, now=now, daily_max=2, cooldown_seconds=900,
        )
        assert not market.reserve_ai_request(
            model=model, now=now + timedelta(seconds=5),
            daily_max=2, cooldown_seconds=900,
        )
        market.save_ai_research(
            asset="BTC", model=model, evidence_sha256="a" * 64,
            issued_at=now, direction="uncertain", confidence="low",
            thesis="Mixed venue returns.", risk="Missing historical depth.",
            source_count=3,
        )
        market.save_ai_research(
            asset="BTC", model=model, evidence_sha256="a" * 64,
            issued_at=now, direction="uncertain", confidence="low",
            thesis="Mixed venue returns.", risk="Missing historical depth.",
            source_count=3,
        )
        cached = market.cached_ai_research(
            asset="BTC", model=model, evidence_sha256="a" * 64,
        )
        assert cached is not None
        assert cached["source_count"] == 3
        market.pause_ai_requests(model=model, now=now, seconds=3600)
        assert not market.reserve_ai_request(
            model=model, now=now + timedelta(minutes=20),
            daily_max=2, cooldown_seconds=900,
        )
        with store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM perp_ai_research WHERE model=?",
                (model,),
            ).fetchone()
            assert count["n"] == 1
    finally:
        with store.connect() as conn:
            conn.executescript(
                """DROP TABLE IF EXISTS perp_ai_research;
                   DROP TABLE IF EXISTS perp_ai_quota;"""
            )
        store.close()


def test_postgres_gemini_pause_survives_utc_midnight():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres integration service not configured")
    store = PostgresHotStore(dsn)
    market = PerpetualMarketStore(store)
    model = "rival-ci-midnight-pause"
    previous = datetime(2026, 10, 28, 23, 58, tzinfo=UTC)
    try:
        market.initialize()
        assert market.reserve_ai_request(
            model=model, now=previous, daily_max=1,
            cooldown_seconds=900,
        )
        market.pause_ai_requests(model=model, now=previous, seconds=3600)
        checked = previous + timedelta(minutes=12)
        state = market.ai_request_status(
            model=model, now=checked, daily_max=1,
            cooldown_seconds=900,
        )
        assert state["requests_today"] == 0
        assert "provider_error_pause" in state["reasons"]
        assert state["retry_at"] == previous + timedelta(hours=1)
        assert not market.reserve_ai_request(
            model=model, now=checked, daily_max=1,
            cooldown_seconds=900,
        )
        assert market.reserve_ai_request(
            model=model, now=previous + timedelta(hours=1),
            daily_max=1, cooldown_seconds=900,
        )
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_ai_quota WHERE model=?", (model,),
            )
        store.close()
