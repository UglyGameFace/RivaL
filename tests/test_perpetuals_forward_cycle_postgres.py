"""PostgreSQL 16: prospective issuance diagnostics persist and update by UTC slot."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore


def test_postgres_forecast_cycle_diagnostics_survive_restart():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration service unavailable")
    store = PostgresHotStore(dsn)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    stamp = datetime(2026, 1, 3, 11, 13, tzinfo=UTC)
    metrics = dict.fromkeys((
        "discovered_streams", "selected_streams", "short_history",
        "stale_last_candle", "invalid_data",
        "insufficient_causal_labels", "insufficient_training",
        "target_expired", "duplicate_forecasts",
        "issued_forecasts", "settled_forecasts",
    ), 0)
    metrics.update({
        "discovered_streams": 3, "selected_streams": 3,
        "short_history": 2, "issued_forecasts": 1,
    })
    slot = stamp.replace(minute=10).isoformat()
    try:
        monitor.initialize()
        monitor._record_cycle(stamp, metrics)
        restarted = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        row = restarted.latest_diagnostics()
        assert row is not None
        assert row["slot_utc"] == slot
        assert row["issued_forecasts"] == 1
        metrics["issued_forecasts"] = 0
        metrics["duplicate_forecasts"] = 1
        restarted._record_cycle(stamp + timedelta(seconds=2), metrics)
        with store.connect() as conn:
            updated = conn.execute(
                """SELECT issued_forecasts,duplicate_forecasts
                   FROM perp_forward_cycle_diagnostics WHERE slot_utc=?""",
                (slot,),
            ).fetchone()
            assert updated["issued_forecasts"] == 0
            assert updated["duplicate_forecasts"] == 1
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_cycle_diagnostics WHERE slot_utc=?",
                (slot,),
            ).fetchone()
            assert count["n"] == 1
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_forward_cycle_diagnostics WHERE slot_utc=?",
                (slot,),
            )
        store.close()
