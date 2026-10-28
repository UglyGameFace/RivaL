"""Same learning ledger against the existing CI PostgreSQL 16 service."""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from parlay_bot.perpetuals.fusion import SourceForecast, fuse_forecasts
from parlay_bot.perpetuals.ledger import PredictionLedger
from parlay_bot.storage.hot import PostgresHotStore


def test_forecasts_are_idempotent_and_source_skill_persists_in_postgres():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration service not configured")
    store = PostgresHotStore(dsn)
    try:
        with store.connect() as conn:
            conn.executescript(
                """DROP TABLE IF EXISTS perp_source_skill;
                   DROP TABLE IF EXISTS perp_predictions;"""
            )
        store.initialize()
        ledger = PredictionLedger(store)
        ledger.initialize()
        now = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)
        observations = [
            SourceForecast(
                source=name, origin=name, asset="BTC", quote="USDC",
                horizon_minutes=5, observed_at=now, probability_up=p,
                quality=.9, reference_price=100,
            )
            for name, p in (("hl", .7), ("dx", .6), ("lt", .8))
        ]
        result = fuse_forecasts(
            observations, issued_at=now, skill_for=ledger.skill_for
        )
        assert result is not None
        assert ledger.record(result)
        assert not ledger.record(result)
        assert ledger.settle(
            result.id, reference_source=result.reference_source,
            observed_at=result.due_at, settlement_price=110,
        )
        assert not ledger.settle(
            result.id, reference_source=result.reference_source,
            observed_at=result.due_at, settlement_price=110,
        )
        assert PredictionLedger(store).skill_for("BTC", "USDC", "hl", 5).count == 1
    finally:
        with store.connect() as conn:
            conn.executescript(
                """DROP TABLE IF EXISTS perp_source_skill;
                   DROP TABLE IF EXISTS perp_predictions;"""
            )
        store.close()
