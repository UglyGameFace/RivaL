"""Say what is growing, not just that memory is high.

Production (Nov 5): after each restart the bot's own RSS climbs about
25 MB an hour until the 512 MB container is OOM-killed (~3 h). The
existing guard only reports "high host RSS". Every 30 minutes this takes
a census of garbage-collected objects by type and, when RSS grew, logs the
types whose counts grew most. Counts and type names only: no values.

Two limits of that census, found while tracing the Nov 5 kill (round-5
P1-15): it can only see garbage-collected objects, and the bot's largest
known in-memory structures are not that shape. A numpy array is not
GC-tracked at all, and neither is a dict or list holding only plain
scalars (str/int/float/bytes) -- which is exactly what the checkpoint
fingerprint memo (str -> (int, str)), the checkpoint "recent" maps
(str -> str) and the window memos hold. So "top_growth=[none]" while RSS
climbs does not exonerate anything; it is the expected output for growth
in those structures. The holder walk below (added in #383) is the part
that can see them, because it walks from live owners instead of from the
GC list -- which is why a receipt that sees a big step now runs the
holders walk on that very receipt instead of waiting for the next
high-RSS slot.
"""
from __future__ import annotations

import gc
import logging
import sys
from collections import Counter

from parlay_bot.perpetuals import host_memory

_LOG = logging.getLogger(__name__)
MIB = 1024 * 1024
GREW_MB = 8
HOLDERS_ABOVE_MB = 350
HOLDERS_EVERY_S = 2 * 3600
# A receipt that sees a real step (>= GREW_MB in 30 minutes, the same
# threshold that warns) runs the holders walk on that receipt: the Nov 5
# kill's biggest step (+89 MB in 30 minutes, no GC count growth) fell
# between the 2-hour high-RSS slots, so no holder line ever named it.
HOLDERS_GROWTH_EVERY_S = 30 * 60
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


# glibc mallopt parameter for the maximum number of malloc arenas.
M_ARENA_MAX = -8
ARENA_CAP = 2


def limit_allocator_arenas(max_arenas: int = ARENA_CAP) -> bool:
    """Cap glibc malloc arenas (glibc only; a no-op elsewhere).

    Measured live 2026-11-26 on the production bot: 642 MB of RSS sat in
    anonymous mappings against 150 MB in the main heap -- 59 anonymous
    regions of 4 MB or more across 23 threads -- while every GC-visible
    holder (path/window memos, checkpoint cache) was flat for hours and
    the post-trim floor still climbed ~20 MB/h to the container kill.
    Freed blocks were stranding in per-thread arenas, which malloc_trim
    cannot drain (it trims only the main arena). Capping arena creation
    forces freed blocks back into a couple of arenas that reuse and the
    trim can actually return to the OS. Most effective before worker
    threads spawn (main.py calls it first); harmless to repeat.
    """
    try:
        import ctypes

        return bool(ctypes.CDLL("libc.so.6").mallopt(M_ARENA_MAX, max_arenas))
    except (OSError, AttributeError):
        return False


def _deep_size(root, seen: set, budget: list) -> int:
    """Bytes reachable from ``root`` not already counted (bounded walk)."""
    total = 0
    stack = [root]
    while stack and budget[0] > 0:
        obj = stack.pop()
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        budget[0] -= 1
        # A test double's __sizeof__ is itself a mock: calling it records a
        # call (new objects) on every visit, so the walk never ends. Skip.
        if getattr(type(obj), "__module__", None) == "unittest.mock":
            continue
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


def memory_holders(top: int = 10, node_budget: int = 4_000_000,
                   ) -> tuple[list[tuple[str, int]], bool]:
    """Largest attributes of RivaL objects: "Class.attr" -> bytes reachable.

    Each byte is counted once, for the first holder walked (largest objects
    are not known in advance, so attribution between shared holders is
    approximate). Names and sizes only, never values.

    Returns ``(holders, partial)``: ``partial`` is True when the node
    budget ran out, so the list may be missing holders and the sizes are
    lower bounds -- a partial list must never be read as "nothing big".
    Text and bytes attributes are counted by their own size (they have no
    referents to walk); skipping them hid multi-megabyte payloads and
    JSON text held on a holder.
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
            if isinstance(value, (int, float, bool, type(None))):
                continue
            if isinstance(value, (str, bytes)):
                size = sys.getsizeof(value)
            else:
                size = _deep_size(value, seen, budget)
            if size >= 512 * 1024:
                key = f"{type(owner).__name__}.{name}"
                sizes[key] = sizes.get(key, 0) + size
    holders = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return holders, budget[0] <= 0


class MemoryReceipt:
    def __init__(self) -> None:
        self._last_counts: Counter | None = None
        self._last_sizes: Counter | None = None
        self._last_rss: int | None = None
        self._holders_at = -HOLDERS_EVERY_S
        # Belt and braces for entry points other than main.py: cap the
        # allocator arenas here too (idempotent; see limit_allocator_arenas).
        self._arenas_capped = limit_allocator_arenas()

    @staticmethod
    def _census() -> tuple[Counter, Counter]:
        """Object counts AND payload bytes per type, in one GC pass.

        Counts alone missed the 2026-11-27 step: the +95 MB interval's
        count leaders were ~880 small tick objects, while the retained
        bytes sat in payloads (a few huge buffers count once each).
        sys.getsizeof reports a container's slots and an array's data
        buffer, so summing it per type makes payload growth through
        GC-tracked carriers visible in the same pass. Atomic payloads
        (bytes/str/floats) are not GC-tracked and native buffers owned
        by C extensions (e.g. DuckDB) are not Python objects at all --
        both stay invisible here, which is itself the discriminator if
        a step shows in neither counts nor bytes.
        """
        counts: Counter = Counter()
        sizes: Counter = Counter()
        for obj in gc.get_objects():
            name = type(obj).__name__
            counts[name] += 1
            try:
                sizes[name] += sys.getsizeof(obj)
            except (TypeError, OverflowError):
                pass
        return counts, sizes

    def sample(self) -> str:
        """One receipt; returns the logged line (for tests)."""
        memory = host_memory.snapshot()
        rss = memory.rss_bytes or 0
        counts, sizes = self._census()
        trimmed = _malloc_trim()
        after = host_memory.snapshot().rss_bytes or rss if trimmed else rss
        rss = after
        grew: int | None = None
        top = ""
        top_bytes = ""
        if self._last_counts is not None and self._last_rss is not None:
            grew = (rss - self._last_rss) // MIB
            delta = counts.copy()
            delta.subtract(self._last_counts)
            top = ", ".join(f"{name}+{n}" for name, n in delta.most_common(TOP) if n > 0)
            if self._last_sizes is not None:
                byte_delta = sizes.copy()
                byte_delta.subtract(self._last_sizes)
                # Only payload-scale moves (>= 1 MB) are signal; the
                # census's own bookkeeping objects churn below that.
                top_bytes = ", ".join(
                    f"{name}{n / MIB:+.1f}" for name, n in byte_delta.most_common(TOP)
                    if n >= MIB)
        self._last_counts, self._last_sizes, self._last_rss = counts, sizes, rss
        # Name what holds it. Above the high-RSS line, at most every 2 hours
        # (a few seconds of CPU, in this worker thread) -- and on any
        # receipt that itself sees a real step, at most every 30 minutes,
        # so a fast climb is attributed on the receipt that saw it instead
        # of two hours later or never. The walk runs BEFORE the receipt
        # line is logged so its results can be stored alongside it: the
        # problem log stores only the first 180 chars of a record
        # (error_log.MAX_TEXT), and the per-rank holder lines log at INFO
        # below pressure, so neither the old line order (growth fields
        # last, always truncated away) nor the separate lines ever
        # reached durable storage -- every stored receipt from the
        # 2026-11-26 RAM kill onward ends mid-word at "top_gr". The
        # holders themselves are emitted as their own line below (see
        # there for why they no longer ride inside the receipt line).
        import time as _time
        now = _time.monotonic()
        high = after // MIB >= HOLDERS_ABOVE_MB
        due = now - self._holders_at >= HOLDERS_EVERY_S
        stepped = (grew is not None and grew >= GREW_MB
                   and now - self._holders_at >= HOLDERS_GROWTH_EVERY_S)
        holders: list[tuple[str, int]] = []
        if (high and due) or stepped:
            self._holders_at = now
            holders, partial = memory_holders(top=6)
            if partial:
                _LOG.warning(
                    "RivaL memory holders partial: node budget exhausted; "
                    "holders may be missing and sizes are lower bounds")
        # Growth attribution FIRST, host detail LAST: the stored copy of
        # this line is truncated at 180 chars, so the fields that name
        # the growth must live inside that budget and the detail (already
        # carried by the pressure warnings) is what gets cut.
        line = f"rss_mb={rss // MIB} after_trim_mb={after // MIB}"
        if grew is not None:
            line += f" grew_mb={grew} top_growth=[{top or 'none'}]"
            if top_bytes:
                line += f" top_bytes=[{top_bytes}]"
        line += (f" objects={sum(counts.values())}"
                 f" arenas={'capped' if self._arenas_capped else 'native'}"
                 f" {memory.detail}")
        if grew is not None and grew >= GREW_MB:
            _LOG.warning("RivaL memory receipt %s", line)
        else:
            _LOG.info("RivaL memory receipt %s", line)
        if holders:
            # The holders list rides on its OWN stored line, not inside
            # the receipt line: the first attributable receipt (2026-11-26
            # 19:53Z) proved the receipt's own top_growth segment eats the
            # whole 180-char storage budget and the holders segment fell
            # off the end -- stored text ended at "hold". A dedicated
            # template keeps the top holders queryable on their own; it
            # logs at warning whenever the receipt itself did (a real
            # growth step) or under pressure, so growth episodes always
            # leave the attribution in durable storage.
            combined = ", ".join(f"{name}={size // MIB}MB"
                                 for name, size in holders[:4])
            if (grew is not None and grew >= GREW_MB) or getattr(
                    memory, "pressure", False):
                _LOG.warning("RivaL memory holders %s", combined)
            else:
                _LOG.info("RivaL memory holders %s", combined)
            # One line per rank, as before: routine attribution is
            # information, not a problem -- at warning level every 2-hour
            # sample landed on the status card as six red lines while
            # memory was a third of the limit. Only when memory is
            # actually tight are the holders a warning.
            level = logging.WARNING if getattr(memory, "pressure", False) else logging.INFO
            for rank, (name, size) in enumerate(holders, start=1):
                _LOG.log(level, f"RivaL memory holder #{rank}: %s=%dMB", name, size // MIB)
        return line


def census_snapshot() -> tuple[int, Counter, Counter]:
    """One (rss_bytes, counts, sizes) census snapshot for before/after pairs.

    The same GC pass MemoryReceipt.sample() uses, exposed so a periodic
    job can bracket its own run with a pair and have the delta attributed
    to it (memory step attribution plan, 2026-11-27). Read-only: it
    counts live objects, it changes nothing.
    """
    counts, sizes = MemoryReceipt._census()
    rss = getattr(host_memory.snapshot(), "rss_bytes", None) or 0
    return rss, counts, sizes


def census_start():
    """Fail-safe before snapshot for job wraps: None when unavailable.

    The wraps are additive logging only, so a census failure must never
    break the job it brackets -- callers pass the result to
    log_job_census(), which treats None as "no bracket".
    """
    try:
        return census_snapshot()
    except Exception:  # noqa: BLE001 - instrumentation must not break jobs
        _LOG.warning("RivaL memory census snapshot unavailable", exc_info=True)
        return None


def census_delta(before, after) -> dict:
    """Delta between two census_snapshot() results, without logging.

    Returns rss_delta_mb (signed whole MB) plus the receipt's own
    top-growth formats: count deltas for the TOP types, and byte deltas
    restricted to payload-scale moves (>= 1 MB), matching sample().
    """
    rss_before, counts_before, sizes_before = before
    rss_after, counts_after, sizes_after = after
    count_delta = counts_after.copy()
    count_delta.subtract(counts_before)
    top_counts = ", ".join(
        f"{name}+{n}" for name, n in count_delta.most_common(TOP) if n > 0)
    byte_delta = sizes_after.copy()
    byte_delta.subtract(sizes_before)
    top_bytes = ", ".join(
        f"{name}{n / MIB:+.1f}" for name, n in byte_delta.most_common(TOP)
        if n >= MIB)
    return {
        "rss_delta_mb": (rss_after - rss_before) // MIB,
        "top_counts": top_counts,
        "top_bytes": top_bytes,
    }


def log_job_census(job: str, before) -> str:
    """Take the after snapshot for ``job`` and log its census delta line.

    One INFO line per run: ``memory census job=<name> rss_delta_mb=<n>
    top_counts=<...> top_bytes=<...>``. Additive logging only -- the
    caller passes the before snapshot from census_start() and its
    control flow is unchanged; a missing or unreadable bracket logs
    nothing and returns "". Returns the logged line (for tests).
    """
    if before is None:
        return ""
    try:
        delta = census_delta(before, census_snapshot())
    except Exception:  # noqa: BLE001 - instrumentation must not break jobs
        _LOG.warning("RivaL memory census for job=%s unavailable", job,
                     exc_info=True)
        return ""
    line = (f"memory census job={job} rss_delta_mb={delta['rss_delta_mb']}"
            f" top_counts=[{delta['top_counts'] or 'none'}]"
            f" top_bytes=[{delta['top_bytes'] or 'none'}]")
    _LOG.info("RivaL %s", line)
    return line
