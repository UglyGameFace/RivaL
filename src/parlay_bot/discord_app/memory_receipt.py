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


class MemoryReceipt:
    def __init__(self) -> None:
        self._last_counts: Counter | None = None
        self._last_rss: int | None = None

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
        return line
