"""Name the code that blocks the Discord event loop.

Discord gives a command 3 seconds to be acknowledged. A command that fails
with "Unknown interaction" (10062) even though its first statement is
``defer()`` means the event loop itself was busy: some synchronous work ran
on it instead of in a thread. That is invisible in the logs, so this watch
records where the loop was when it stalled.

A loop task stamps a heartbeat every ``TICK`` seconds. A daemon thread
checks the stamp; when it is older than ``STALL_AFTER`` it captures the
loop thread's current stack once per stall and logs the innermost RivaL
frames (file:line and function only: no locals, arguments or values).
Overhead is one tiny task wake-up and one thread check per tick.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

_LOG = logging.getLogger(__name__)

TICK = 0.25
STALL_AFTER = 1.0
# Discord's acknowledgement window is 3 s; a stall this long can fail taps.
SLOW_STALL = 2.5
FRAMES = 4


def _short(f) -> str:
    name = f.filename
    for marker in ("parlay_bot/", "site-packages/", "lib/python3"):
        if marker in name:
            name = name.rsplit(marker, 1)[-1]
            break
    return f"{name}:{f.lineno} {f.name}"


def _location(thread_id: int, frames=None) -> str:
    """Innermost frame (any library) plus the innermost RivaL frames."""
    frame = (frames or sys._current_frames()).get(thread_id)
    if frame is None:
        return "unknown"
    stack = traceback.extract_stack(frame)
    ours = [f for f in stack if "parlay_bot" in f.filename
            and not f.filename.endswith("loop_watch.py")]
    picked = ours[-FRAMES:]
    parts = [_short(f) for f in reversed(picked)]
    if stack and (not picked or stack[-1] is not picked[-1]):
        parts.insert(0, "in " + _short(stack[-1]))
    return " <- ".join(parts) or "unknown"


_IDLE = ("wait", "_wait_for_tstate_lock", "get", "select", "poll", "_worker", "sleep",
         "accept", "recv_into", "read")


def _busy_threads(loop_thread: int, frames) -> str:
    """Other threads running RivaL Python right now: a CPU-bound thread holding
    the GIL also starves the loop, and then the loop's own frame is incidental."""
    names = {t.ident: t.name for t in threading.enumerate()}
    busy = []
    for ident, frame in frames.items():
        if ident in (loop_thread, threading.get_ident()):
            continue
        stack = traceback.extract_stack(frame)
        if not stack or stack[-1].name in _IDLE:
            continue
        ours = [f for f in stack if "parlay_bot" in f.filename]
        if ours:
            busy.append(f"{names.get(ident, ident)}: {_short(ours[-1])}")
    return "; ".join(busy[:3]) or "none"


class LoopStallWatch:
    def __init__(self, *, stall_after: float = STALL_AFTER, tick: float = TICK) -> None:
        self.stall_after = stall_after
        self.tick = tick
        self._beat = time.monotonic()
        self._loop_thread: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stalls: list[tuple[float, str]] = []  # (seconds, location), bounded

    async def run(self) -> None:
        """The loop-side heartbeat; cancel it to stop the watch."""
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._thread = threading.Thread(target=self._watch, name="rival-loop-stall-watch",
                                        daemon=True)
        self._thread.start()
        try:
            while True:
                self._beat = time.monotonic()
                await asyncio.sleep(self.tick)
        finally:
            self._stop.set()

    def _watch(self) -> None:
        reported_for = None
        longest = 0.0
        while not self._stop.wait(self.tick):
            beat = self._beat
            late = time.monotonic() - beat
            if reported_for is not None and reported_for != beat:
                # The stall ended: record how long it really lasted. Discord
                # fails an unacknowledged interaction after 3 seconds, so a
                # stall near that length is a warning, not just a sample.
                total = round(max(longest, beat - reported_for), 1)
                self.stalls = (self.stalls[:-1] + [(total, self.stalls[-1][1])]
                               if self.stalls else [])
                (_LOG.warning if total >= SLOW_STALL else _LOG.info)(
                    "RivaL event loop stall lasted %.1fs", total)
                reported_for, longest = None, 0.0
            if late < self.stall_after:
                continue
            longest = max(longest, late)
            if reported_for == beat or self._loop_thread is None:
                continue
            reported_for = beat
            frames = sys._current_frames()
            where = _location(self._loop_thread, frames)
            busy = _busy_threads(self._loop_thread, frames)
            self.stalls = (self.stalls + [(round(late, 1), where)])[-20:]
            _LOG.warning("RivaL event loop blocked >%.1fs at %s | busy threads: %s",
                         self.stall_after, where, busy)
