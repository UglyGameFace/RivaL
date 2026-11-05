"""The memory receipt names the object types that grew, without values."""
import logging

from parlay_bot.discord_app import memory_receipt
from parlay_bot.discord_app.memory_receipt import MemoryReceipt


class _Snap:
    def __init__(self, rss):
        self.rss_bytes = rss
        self.detail = "cgroup_mb=1"


def test_growth_is_reported_with_the_types_that_grew(monkeypatch, caplog):
    readings = iter([100 * 2**20, 120 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot", lambda: _Snap(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    receipt = MemoryReceipt()
    receipt.sample()

    class LeakyThing:
        pass

    hoard = [LeakyThing() for _ in range(5000)]
    with caplog.at_level(logging.WARNING, logger="parlay_bot.discord_app.memory_receipt"):
        line = receipt.sample()
    assert "grew_mb=20" in line and "LeakyThing+" in line
    assert any("memory receipt" in r.getMessage() for r in caplog.records)
    del hoard


def test_malloc_trim_is_safe_to_call():
    assert memory_receipt._malloc_trim() in (True, False)
