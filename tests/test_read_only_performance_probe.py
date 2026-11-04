"""The production diagnostics cannot initialize schemas or change live labels."""
from __future__ import annotations

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore
from tools.performance_truth_probe import read_only_forward_monitor


def test_forward_monitor_read_only_does_not_create_or_rewrite_tables(tmp_path):
    store = SQLiteHotStore(tmp_path / "read-only.sqlite")
    monitor = read_only_forward_monitor(store)
    assert isinstance(monitor, ForwardAccuracyMonitor)
    assert monitor._read_only is True
    assert isinstance(monitor.market, PerpetualMarketStore)
    monitor.initialize()
    assert monitor.market._forward_schema_ready is False
    with store.connect() as conn:
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    store.close()
