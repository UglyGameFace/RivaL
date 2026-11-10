"""The prediction stage's deadline must sit above its normal run time.

It normally takes 31-92 s on production. At 90 s the wait gave up on runs
that were about to finish: forecasts were stored but the cycle was recorded
as a failed stage. The alert also has to say which stage failed.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app import bot as bot_module
from parlay_bot.perpetuals import autonomous as autonomous_module
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage import query_scope
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_rivala_cycle_timeouts import _research

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_the_limit_clears_a_normal_run_and_still_fits_its_bounds():
    limit = autonomous_module.AUTONOMOUS_FORWARD_TIMEOUT_SECONDS
    assert limit >= 150                                          # 92 s seen, with room
    assert limit * 1000 > query_scope.BACKGROUND_STATEMENT_TIMEOUT_MS  # statements abort first
    assert limit < bot_module.PERP_FORECAST_TIMEOUT_SECONDS      # under the whole-cycle cap


@pytest.mark.asyncio
async def test_a_timed_out_prediction_stage_says_where_its_time_went(monkeypatch, caplog):
    blocked = threading.Event()

    def run_once():
        blocked.wait()
        return 0, 0

    system, saved = _research(monkeypatch, forward=run_once)
    system.forward.last_timing = {"read": 12.4, "fit": 61.0, "write": 9.6}
    monkeypatch.setattr(autonomous_module, "AUTONOMOUS_FORWARD_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.WARNING, logger="parlay_bot.perpetuals.autonomous"):
        await asyncio.wait_for(system.cycle(), timeout=5)
    assert saved["math_ok"] is False
    assert "seconds per phase so far: read=12 fit=61 write=10" in caplog.text
    blocked.set()
    await asyncio.sleep(0.05)


@pytest.mark.parametrize(("flags", "named"), [
    ((0, 1, 1), "prediction"),
    ((1, 0, 1), "AI grading"),
    ((1, 1, 0), "hourly model review"),
    ((0, 1, 0), "prediction, hourly model review"),
])
def test_the_alert_names_the_stage_that_failed(tmp_path, flags, named):
    store = SQLiteHotStore(tmp_path / "stage.sqlite")
    store.initialize()
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        with store.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
                math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
                math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
                model_review_ok INTEGER NOT NULL, auto_ai_ok INTEGER NOT NULL)""")
            at = (NOW - timedelta(minutes=2)).isoformat()
            conn.execute(
                "INSERT INTO perp_learning_cycle_health VALUES (?,?,?,?,?,?,?,?,?,?)",
                (at, at, at, 100, 100, 0, *flags, 1))
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        faults = monitor.inspect(now=NOW, learning_enabled=True, regressions_inline=False)
        assert faults["learning_failure"] == f"persisted learning stage failed: {named}"
    finally:
        store.close()


def test_a_clean_cycle_raises_no_failure(tmp_path):
    store = SQLiteHotStore(tmp_path / "clean.sqlite")
    store.initialize()
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        with store.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
                math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
                math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
                model_review_ok INTEGER NOT NULL, auto_ai_ok INTEGER NOT NULL)""")
            at = (NOW - timedelta(minutes=2)).isoformat()
            conn.execute(
                "INSERT INTO perp_learning_cycle_health VALUES (?,?,?,?,?,?,?,?,?,?)",
                (at, at, at, 100, 100, 0, 1, 1, 1, 1))
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        assert "learning_failure" not in monitor.inspect(
            now=NOW, learning_enabled=True, regressions_inline=False)
    finally:
        store.close()


