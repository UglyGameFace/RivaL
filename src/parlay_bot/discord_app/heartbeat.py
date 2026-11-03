"""Outside-in liveness: ping Healthchecks.io only while the bot really works.

Each minute the bot runs a tiny database query on a worker thread and then
pings the check URL (or ``/fail`` when the database check failed). A frozen
event loop, a crashed process or a dead host sends nothing, and Healthchecks.io
alerts after its grace period. The in-process watchdog cannot detect those.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

_LOG = logging.getLogger(__name__)


class HealthchecksHeartbeat:
    def __init__(
        self, url: str, store, *, interval_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cleaned = url.strip().rstrip("/")
        if not cleaned.startswith("https://"):
            raise ValueError("Healthchecks ping URL must use https")
        self._url = cleaned
        self._store = store
        self.interval_seconds = interval_seconds
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10))
        self._owned_client = client is None
        # Read by the Discord status card; never includes the URL.
        self.last_success_at: datetime | None = None
        self.last_problem_at: datetime | None = None
        self.last_problem: str | None = None
        self.consecutive_problems = 0

    def _database_ok(self) -> None:
        with self._store.connect() as conn:
            conn.execute("SELECT 1 AS ok").fetchone()

    async def beat_once(self) -> bool:
        """Ping success when the database answers within 10 s, else /fail."""
        problem = None
        try:
            await asyncio.wait_for(asyncio.to_thread(self._database_ok), timeout=10)
            healthy = True
        except Exception as exc:  # noqa: BLE001 - any failure is reported, not raised
            _LOG.warning("RivaL heartbeat database check failed %s", type(exc).__name__)
            healthy = False
            problem = f"database check failed ({type(exc).__name__})"
        target = self._url if healthy else self._url + "/fail"
        try:
            response = await self._client.post(
                target, content=b"database=ok" if healthy else b"database=failed",
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never log the URL: its UUID is the check's only credential.
            _LOG.warning("RivaL heartbeat ping failed %s", type(exc).__name__)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            problem = problem or (
                f"ping not delivered (HTTP {status})" if status
                else f"ping not delivered ({type(exc).__name__})")
        now = datetime.now(UTC)
        if problem is None:
            self.last_success_at = now
            self.consecutive_problems = 0
        else:
            self.last_problem_at, self.last_problem = now, problem
            self.consecutive_problems += 1
        return healthy

    async def run(self, is_closed: Callable[[], bool]) -> None:
        while not is_closed():
            await self.beat_once()
            await asyncio.sleep(self.interval_seconds)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()
