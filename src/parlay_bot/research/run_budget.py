"""Time and storage budgets for bulk official-archive collection runs.

Static provider archives (for example data.binance.vision) are not the
rate-limited REST APIs, so a run is bounded by wall-clock time and Drive
space rather than a small file count. Requests stay sequential and paced.
"""
from __future__ import annotations

import asyncio
import time

# Leave 10% of the Drive account free for everything else the owner stores.
DRIVE_MAX_USED_FRACTION = 0.90
# Sequential polite pacing between archive requests (about 4 per second).
ARCHIVE_REQUEST_SPACING_SECONDS = 0.25


class RunBudget:
    """Wall-clock deadline for one collection run."""

    def __init__(self, seconds: float, *, clock=time.monotonic) -> None:
        if not 60 <= seconds <= 5 * 3600:
            raise ValueError("run time budget must be 1 minute to 5 hours")
        self._clock = clock
        self._deadline = clock() + seconds
        self.seconds = seconds

    def expired(self) -> bool:
        return self._clock() >= self._deadline

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())


class Pacer:
    """Minimum spacing between consecutive requests to one archive host."""

    def __init__(self, spacing: float = ARCHIVE_REQUEST_SPACING_SECONDS,
                 *, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        if not 0 <= spacing <= 10:
            raise ValueError("invalid archive request spacing")
        self._spacing = spacing
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    async def wait(self) -> None:
        if self._last is not None:
            delay = self._spacing - (self._clock() - self._last)
            if delay > 0:
                await self._sleep(delay)
        self._last = self._clock()


async def drive_has_room(drive, *, max_used_fraction: float = DRIVE_MAX_USED_FRACTION) -> bool:
    """False only when Drive reports usage at or above the allowed fraction."""
    reader = getattr(drive, "storage_quota", None)
    if reader is None:
        return True
    quota = await reader()
    if quota is None:
        return True
    used, limit = quota
    return limit is None or used < limit * max_used_fraction
