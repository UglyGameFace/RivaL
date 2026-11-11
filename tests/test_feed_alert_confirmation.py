"""A feed that misses one check is a warning; still failing 10 minutes on, an alert.

RIVAL-13/14: okx and gate BTC each missed a single check and cleared on the
next; with per-alert Sentry issues every such blip emailed the owner.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore

FEED = "feed:okx:BTC"


@pytest.fixture
def monitor(tmp_path):
    store = SQLiteHotStore(tmp_path / "alerts.sqlite")
    yield RuntimeHealthMonitor(store)
    store.close()


def _errors(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR and "HEALTH ALERT" in r.getMessage()]


def test_a_one_check_blip_never_becomes_an_alert(monitor, caplog):
    t0 = monitor.started_at + timedelta(minutes=30)
    with caplog.at_level(logging.WARNING, logger="parlay_bot.perpetuals.runtime_health"):
        monitor.publish({FEED: "no verified fresh completed candle"}, now=t0)
        monitor.publish({}, now=t0 + timedelta(minutes=2))          # caught up
    assert _errors(caplog) == []
    assert any("HEALTH ALERT feed:okx:BTC" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_a_real_outage_alerts_once_after_ten_minutes(monitor, caplog):
    t0 = monitor.started_at + timedelta(minutes=30)
    fault = {FEED: "no verified fresh completed candle"}
    with caplog.at_level(logging.WARNING, logger="parlay_bot.perpetuals.runtime_health"):
        for minutes in (0, 2, 4, 8):
            monitor.publish(fault, now=t0 + timedelta(minutes=minutes))
        assert _errors(caplog) == []
        monitor.publish(fault, now=t0 + timedelta(minutes=10))
        monitor.publish(fault, now=t0 + timedelta(minutes=12))
        assert _errors(caplog) == [(
            "RivaL HEALTH ALERT feed:okx:BTC: no verified fresh completed candle "
            "(still failing after 10 min)")]
        # Recovered, then failing again: a fresh 10-minute wait, then one alert.
        monitor.publish({}, now=t0 + timedelta(minutes=14))
        monitor.publish(fault, now=t0 + timedelta(minutes=16))
        monitor.publish(fault, now=t0 + timedelta(minutes=27))
    assert len(_errors(caplog)) == 2


def test_other_faults_still_alert_at_once(monitor, caplog):
    t0 = monitor.started_at + timedelta(minutes=30)
    with caplog.at_level(logging.WARNING, logger="parlay_bot.perpetuals.runtime_health"):
        monitor.publish({"learning_stalled": "last completed cycle 20m ago"}, now=t0)
    assert _errors(caplog) == ["RivaL HEALTH ALERT learning_stalled: last completed cycle 20m ago"]
