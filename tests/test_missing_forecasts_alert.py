"""'No forecasts issued' only when nothing explains it (RIVAL-Z).

The first cycle after a restart re-runs the bar the old process already
predicted: every forecast is a duplicate. That is a reason, not a gap.
A cycle whose every prediction ran too late is a real fault with its own name.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.fixture
def monitor(tmp_path):
    store = SQLiteHotStore(tmp_path / "health.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    ForwardAccuracyMonitor(market).initialize()
    with store.connect() as conn:  # the learning loop's own table (autonomous.py)
        conn.execute("""CREATE TABLE perp_learning_cycle_health (
            slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
            math_issued INTEGER NOT NULL, math_settled INTEGER NOT NULL,
            ai_issued INTEGER NOT NULL, math_ok INTEGER NOT NULL,
            ai_settlement_ok INTEGER NOT NULL, model_review_ok INTEGER NOT NULL,
            auto_ai_ok INTEGER NOT NULL)""")
    yield RuntimeHealthMonitor(store)
    store.close()


def _cycle(monitor, at, *, issued=0, duplicate=0, expired=0):
    with monitor.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_cycle_diagnostics (slot_utc,checked_at,
                 discovered_streams,selected_streams,short_history,stale_last_candle,
                 invalid_data,insufficient_causal_labels,insufficient_training,
                 target_expired,duplicate_forecasts,issued_forecasts,settled_forecasts)
               VALUES (?,?,36,36,0,0,0,0,0,?,?,?,0)""",
            (at.isoformat(), at.isoformat(), expired, duplicate, issued))


def _faults(monitor, at):
    return monitor.inspect(now=at, learning_enabled=True)


@pytest.mark.parametrize(("kw", "expected", "absent"), [
    ({}, "missing_forecasts", "forecasts_late"),
    ({"duplicate": 36}, None, "missing_forecasts"),
    ({"expired": 36}, "forecasts_late", "missing_forecasts"),
    ({"issued": 36}, None, "missing_forecasts"),
])
def test_only_an_unexplained_empty_cycle_is_missing_forecasts(monitor, kw, expected, absent):
    at = monitor.started_at + timedelta(minutes=20)
    _cycle(monitor, at - timedelta(minutes=1), **kw)
    faults = _faults(monitor, at)
    assert absent not in faults
    if expected:
        assert expected in faults
    else:
        assert "forecasts_late" not in faults
    if expected == "forecasts_late":
        assert faults["forecasts_late"] == "every prediction this cycle ran too late (36 skipped)"
