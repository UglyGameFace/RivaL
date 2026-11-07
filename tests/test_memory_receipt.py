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


def test_memory_holders_names_the_attribute_that_holds_the_bytes():
    from parlay_bot.discord_app.memory_receipt import memory_holders
    from parlay_bot.storage import hot

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.big_cache = {i: list(range(50)) for i in range(2000)}
    holders, partial = memory_holders(top=20)
    assert not partial
    names = dict(holders)
    assert any(name == "Holder.big_cache" for name in names), names
    assert names["Holder.big_cache"] > 512 * 1024
    del holder, hot


def test_memory_holders_does_not_loop_on_test_doubles():
    from unittest.mock import MagicMock

    from parlay_bot.discord_app.memory_receipt import memory_holders

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.double = MagicMock()
    holder.cache = {i: list(range(50)) for i in range(2000)}
    holders, partial = memory_holders(top=20)  # must finish, not walk the mock forever
    assert not partial
    names = dict(holders)
    assert "Holder.cache" in names
    del holder


def test_growth_step_triggers_holders_below_high_rss(monkeypatch, caplog):
    """The Nov 5 kill's +89 MB step fell between the 2-hour high-RSS slots.

    A receipt that itself sees >=8 MB growth must run the holders walk on
    that receipt, even while RSS is still below the 350 MB high line.
    """
    readings = iter([100 * 2**20, 120 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot", lambda: _Snap(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    receipt = MemoryReceipt()
    receipt.sample()

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.big_cache = {i: list(range(50)) for i in range(2000)}
    with caplog.at_level(logging.WARNING, logger="parlay_bot.discord_app.memory_receipt"):
        line = receipt.sample()
    assert "grew_mb=20" in line
    assert any("Holder.big_cache" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    del holder


def test_memory_holders_names_bytes_and_text_attributes():
    """A holder keeping megabytes of payload bytes or JSON text is a holder.

    The walk used to skip str/bytes attributes outright, so a raw payload
    or cached document was invisible to attribution.
    """
    from parlay_bot.discord_app.memory_receipt import memory_holders

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.payload = b"x" * (2 * 1024 * 1024)
    holder.document = "y" * (2 * 1024 * 1024)
    holders, partial = memory_holders(top=20)
    assert not partial
    names = dict(holders)
    assert names["Holder.payload"] >= 2 * 1024 * 1024
    assert names["Holder.document"] >= 2 * 1024 * 1024
    del holder


def test_memory_holders_reports_a_partial_walk():
    """A budget-exhausted walk says so; a partial list is not "nothing big"."""
    from parlay_bot.discord_app.memory_receipt import memory_holders

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.cache = {i: list(range(50)) for i in range(2000)}
    _, partial = memory_holders(top=20, node_budget=10)
    assert partial
    del holder
