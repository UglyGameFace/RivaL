"""Say what is growing, not just that memory is high.

Production (Nov 5): after each restart the bot's own RSS climbs about
25 MB an hour until the 512 MB container is OOM-killed (~3 h). The
existing guard only reports "high host RSS". Every 30 minutes this takes
a census of garbage-collected objects by type and, when RSS grew, logs the
types whose counts grew most. Counts and type names only: no values.
"""
from __future__ import annotations

import gc
import logging
from collections import Counter

from parlay_bot.perpetuals import host_memory

_LOG = logging.getLogger(__name__)
MIB = 1024 * 1024
GREW_MB = 8
HOLDERS_ABOVE_MB = 350
HOLDERS_EVERY_S = 2 * 3600
TOP = 8


def _malloc_trim() -> bool:
    """Return freed C-heap pages to the OS (glibc only; a no-op elsewhere).

    Many worker threads each get their own glibc malloc arena; memory Python
    frees stays mapped in them, so RSS can grow while live objects do not.
    The receipt reports RSS before and after, which tells the two apart.
    """
    try:
        import ctypes

        return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except (OSError, AttributeError):
        return False


def _deep_size(root, seen: set, budget: list) -> int:
    """Bytes reachable from ``root`` not already counted (bounded walk)."""
    import sys

    total = 0
    stack = [root]
    while stack and budget[0] > 0:
        obj = stack.pop()
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        budget[0] -= 1
        try:
            total += sys.getsizeof(obj)
        except TypeError:
            continue
        if isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        elif hasattr(obj, "__dict__") and not isinstance(obj, type):
            stack.append(vars(obj))
        elif hasattr(obj, "__slots__"):
            for slot in getattr(type(obj), "__slots__", ()):
                if hasattr(obj, slot):
                    stack.append(getattr(obj, slot))
    return total


def memory_holders(top: int = 10, node_budget: int = 4_000_000) -> list[tuple[str, int]]:
    """Largest attributes of RivaL objects: "Class.attr" -> bytes reachable.

    Each byte is counted once, for the first holder walked (largest objects
    are not known in advance, so attribution between shared holders is
    approximate). Names and sizes only, never values.
    """
    seen: set = set()
    budget = [node_budget]
    sizes: dict[str, int] = {}
    def _ours(o) -> bool:
        module = getattr(type(o), "__module__", "")
        # Some C types expose __module__ as a descriptor, not a string.
        return (isinstance(module, str) and module.startswith("parlay_bot")
                and not isinstance(o, type) and isinstance(getattr(o, "__dict__", None), dict))

    owners = [o for o in gc.get_objects() if _ours(o)]
    for owner in owners:
        seen.add(id(owner))
        seen.add(id(vars(owner)))
    for owner in owners:
        for name, value in list(vars(owner).items()):
            if isinstance(value, (int, float, bool, str, bytes, type(None))):
                continue
            size = _deep_size(value, seen, budget)
            if size >= 512 * 1024:
                key = f"{type(owner).__name__}.{name}"
                sizes[key] = sizes.get(key, 0) + size
    return sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:top]


class MemoryReceipt:
    def __init__(self) -> None:
        self._last_counts: Counter | None = None
        self._last_rss: int | None = None
        self._holders_at = -HOLDERS_EVERY_S

    @staticmethod
    def _census() -> Counter:
        return Counter(type(obj).__name__ for obj in gc.get_objects())

    def sample(self) -> str:
        """One receipt; returns the logged line (for tests)."""
        memory = host_memory.snapshot()
        rss = memory.rss_bytes or 0
        counts = self._census()
        trimmed = _malloc_trim()
        after = host_memory.snapshot().rss_bytes or rss if trimmed else rss
        line = (f"rss_mb={rss // MIB} after_trim_mb={after // MIB} {memory.detail} "
                f"objects={sum(counts.values())}")
        rss = after
        if self._last_counts is not None and self._last_rss is not None:
            grew = (rss - self._last_rss) // MIB
            delta = counts.copy()
            delta.subtract(self._last_counts)
            top = ", ".join(f"{name}+{n}" for name, n in delta.most_common(TOP) if n > 0)
            line += f" grew_mb={grew} top_growth=[{top or 'none'}]"
            if grew >= GREW_MB:
                _LOG.warning("RivaL memory receipt %s", line)
            else:
                _LOG.info("RivaL memory receipt %s", line)
        else:
            _LOG.info("RivaL memory receipt %s", line)
        self._last_counts, self._last_rss = counts, rss
        # Once memory is high, name what holds it (a few seconds of CPU, so at
        # most every 2 hours, in this worker thread).
        import time as _time
        if (after // MIB >= HOLDERS_ABOVE_MB
                and _time.monotonic() - self._holders_at >= HOLDERS_EVERY_S):
            self._holders_at = _time.monotonic()
            # One line per rank: the problem log keeps one sample per message
            # template and shortens long text, so a single joined line was cut.
            for rank, (name, size) in enumerate(memory_holders(top=6), start=1):
                _LOG.warning(f"RivaL memory holder #{rank}: %s=%dMB", name, size // MIB)
        return line
