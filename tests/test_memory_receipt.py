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


def test_limit_allocator_arenas_sets_the_glibc_cap(monkeypatch):
    """The arena cap is the retention fix for the 2026-11-26 kill: freed
    blocks stranded in per-thread arenas (642 MB anonymous vs 150 MB main
    heap, holders flat) that malloc_trim cannot drain."""
    import ctypes

    calls = []

    class FakeLibc:
        def mallopt(self, param, value):
            calls.append((param, value))
            return 1

    monkeypatch.setattr(ctypes, "CDLL", lambda name: FakeLibc())
    assert memory_receipt.limit_allocator_arenas() is True
    assert calls == [(memory_receipt.M_ARENA_MAX, memory_receipt.ARENA_CAP)]


def test_limit_allocator_arenas_is_a_safe_noop_without_glibc(monkeypatch):
    import ctypes

    def no_libc(name):
        raise OSError("no libc here")

    monkeypatch.setattr(ctypes, "CDLL", no_libc)
    assert memory_receipt.limit_allocator_arenas() is False


def test_byte_census_names_payload_growth_counts_cannot_see(monkeypatch):
    """The 2026-11-27 +95 MB step's count leaders were ~880 small tick
    objects -- the retained bytes sat in payloads, where one object
    counts once no matter how many megabytes it carries. The receipt
    must report per-type BYTE growth alongside the counts."""
    readings = iter([100 * 2**20, 120 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot", lambda: _Snap(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    receipt = MemoryReceipt()
    receipt.sample()
    # A GC-tracked payload carrier: bytes/str themselves are not
    # GC-tracked, so the census sees payloads through their containers
    # and array buffers -- a 4M-slot list carries ~32 MB of slots.
    payload = [None] * (4 * 2**20)
    line = receipt.sample()
    assert "top_bytes=[list+" in line
    del payload


def test_receipt_line_marks_the_arena_cap_state(monkeypatch):
    """Every stored receipt proves whether the running instance booted
    with the arena cap applied."""
    readings = iter([100 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot", lambda: _Snap(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    monkeypatch.setattr(memory_receipt, "limit_allocator_arenas", lambda: True)
    line = MemoryReceipt().sample()
    assert "arenas=capped" in line


def test_growth_attribution_survives_the_stored_text_budget(monkeypatch):
    """The problem log stores only the first 180 chars of a record.

    The receipt used to put the host detail before grew_mb/top_growth,
    so every stored receipt was truncated mid-word and the growth
    attribution never reached durable storage (found on the receipts
    from the 2026-11-26 RAM kill). Growth fields must come first.
    """
    class VerboseSnap:
        def __init__(self, rss):
            self.rss_bytes = rss
            self.detail = " ".join(f"detail{i}" for i in range(40))

    readings = iter([100 * 2**20, 130 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot",
                        lambda: VerboseSnap(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    receipt = MemoryReceipt()
    receipt.sample()

    class LeakyThing:
        pass

    hoard = [LeakyThing() for _ in range(5000)]
    line = receipt.sample()
    assert line.index("top_growth=[") < 180
    assert "LeakyThing+" in line[:180]
    del hoard


def test_stepped_walk_top_holder_lands_on_the_stored_holders_line(monkeypatch, caplog):
    """A growth step's holders walk must name its top holder on the
    dedicated holders line at warning level: the per-rank lines log at
    INFO below pressure and are never stored, and the receipt line's own
    top_growth segment eats the whole 180-char storage budget (proven
    live 2026-11-26: the stored receipt ended at "hold"), so the holders
    list needs its own stored line to reach durable storage."""
    readings = iter([100 * 2**20, 120 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot", lambda: _Snap(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    receipt = MemoryReceipt()
    receipt.sample()

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.big_cache = {i: list(range(50)) for i in range(4000)}
    with caplog.at_level(logging.INFO, logger="parlay_bot.discord_app.memory_receipt"):
        line = receipt.sample()
    assert "holders=[" not in line
    combined = [r for r in caplog.records
                if r.getMessage().startswith("RivaL memory holders ")]
    assert combined, [r.getMessage() for r in caplog.records]
    assert "Holder.big_cache=" in combined[0].getMessage()
    assert combined[0].levelname == "WARNING"  # grew 20 MB: stored, not INFO
    assert len(combined[0].getMessage()) <= 180
    del holder


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
    with caplog.at_level(logging.INFO, logger="parlay_bot.discord_app.memory_receipt"):
        line = receipt.sample()
    assert "grew_mb=20" in line
    # The per-rank holder lines specifically (the combined holders line is
    # asserted separately in the stepped-walk test above).
    holders = [r for r in caplog.records
               if "RivaL memory holder #" in r.getMessage()
               and "Holder.big_cache" in r.getMessage()]
    assert holders, [r.getMessage() for r in caplog.records]
    # Attribution is information while memory is fine: it must not land on
    # the status card as a red warning. The growth line itself still does.
    assert {r.levelname for r in holders} == {"INFO"}
    assert any(r.levelname == "WARNING" and "grew_mb=20" in r.getMessage()
               for r in caplog.records)
    del holder


def test_holders_are_a_warning_only_when_memory_is_tight(monkeypatch, caplog):
    class Tight(_Snap):
        pressure = True

    readings = iter([100 * 2**20, 120 * 2**20])
    monkeypatch.setattr(memory_receipt.host_memory, "snapshot",
                        lambda: Tight(next(readings)))
    monkeypatch.setattr(memory_receipt, "_malloc_trim", lambda: False)
    receipt = MemoryReceipt()
    receipt.sample()

    class Holder:
        pass

    Holder.__module__ = "parlay_bot.test_fixture"
    holder = Holder()
    holder.big_cache = {i: list(range(50)) for i in range(2000)}
    with caplog.at_level(logging.INFO, logger="parlay_bot.discord_app.memory_receipt"):
        receipt.sample()
    holders = [r for r in caplog.records if "Holder.big_cache" in r.getMessage()]
    assert holders and {r.levelname for r in holders} == {"WARNING"}
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
