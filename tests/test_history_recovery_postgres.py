"""PostgreSQL 16 bounded history audit and recovery cursor contract."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore


def test_postgres_history_gap_audit_and_resume_cursor_do_not_change_legacy():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL CI service")
    store = PostgresHotStore(dsn)
    market = PerpetualMarketStore(store)
    audit = SixMonthQualityAudit(market)
    run = GateHistoryRecovery(market, assets=("SOL",))
    clock = datetime(2026, 10, 28, 12, tzinfo=UTC)
    try:
        audit.initialize()
        result = audit.audit_stream(venue="gate", asset="SOL", now=clock)
        assert result["expected_bars"] > 50_000
        assert result["missing_bars"] >= 0
        assert audit.latest(asset="SOL", venues=("gate",))[0]["status"] == (
            result["status"]
        )
        state = run._position("SOL", clock)
        assert state["started_from"] == state["next_opened_at"]
        later = datetime.fromisoformat(state["next_opened_at"]) + timedelta(hours=2)
        run._advance(
            "SOL", now=clock, next_open=later, inserted=2, status="progress",
        )
        assert run._position("SOL", clock)["next_opened_at"] == later.isoformat()
        # A delayed worker cannot move cursor backwards.
        run._advance(
            "SOL", now=clock, next_open=later - timedelta(hours=1),
            inserted=0, status="progress",
        )
        assert run._position("SOL", clock)["next_opened_at"] == later.isoformat()
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_history_recovery WHERE venue='gate' AND asset='SOL'"
            )
            conn.execute(
                "DELETE FROM perp_history_quality WHERE venue='gate' AND asset='SOL'"
            )
        store.close()
