"""Read-only hosted sports recovery proof never invents result progress."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from parlay_bot.research.database_integrity import sports_runtime_snapshot
from parlay_bot.research.sports_results import SportsResultStore
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_sports_settlement_recovery import event, insert_pending_forecast


def test_hosted_snapshot_shows_actual_results_and_unsettled_counts(tmp_path):
    store = SQLiteHotStore(tmp_path / "runtime.sqlite")
    sports = SportsResultStore(store)
    try:
        store.initialize()
        sports.initialize()
        now = datetime(2026, 10, 21, tzinfo=UTC)
        empty = sports_runtime_snapshot(store, now=now)
        assert empty["final_events"] == 0
        assert empty["scored_forecasts"] == 0
        assert empty["last_cycle"] is None

        sports.save_finalized(event(), observed_at=now)
        insert_pending_forecast(store)
        snapshot = sports_runtime_snapshot(store, now=now)
        assert snapshot["final_events"] == 1
        assert snapshot["unsettled_finalized"] == 1
        assert snapshot["scored_forecasts"] == 0

        store.set_runtime_state("rival:sports-results:last-cycle",
            '{"at":"2026-10-21T00:05:00+00:00","provider":"quota_deferred","new_finals":0,"reconciled":0}')
        quota = sports_runtime_snapshot(store, now=now)
        assert quota["last_cycle"]["provider"] == "quota_deferred"
        assert quota["unsettled_finalized"] == 1
        # Runtime status is not proof of a score or successful provider fetch.
        assert quota["scored_forecasts"] == 0
    finally:
        store.close()


def test_malformed_runtime_state_does_not_poison_immutable_counts(tmp_path):
    store = SQLiteHotStore(tmp_path / "runtime-bad.sqlite")
    try:
        store.initialize()
        SportsResultStore(store).initialize()
        store.set_runtime_state("rival:sports-results:last-cycle", "not-json")
        snapshot = sports_runtime_snapshot(store)
        assert snapshot["last_cycle"] is None
        assert snapshot["final_events"] == 0
        with pytest.raises(ValueError):
            sports_runtime_snapshot(
                store, now=datetime(2026, 10, 21),
            )
    finally:
        store.close()
