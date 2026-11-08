"""Regression test: the Polymarket learning loop must check in on liveness,
not on study success (RIVAL-9).

The Sentry cron monitor ``rival-polymarket-learning`` failed while the loop
was alive: ``heartbeat()`` only fired inside ``if any(studied)``, so a pass
where every study timed out (8 s wait budget) or was cap-rejected sent no
check-in even though the loop itself was healthy.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from parlay_bot.discord_app import bot as bot_module
from parlay_bot.discord_app.bot import RivalDiscordClient

HEARTBEAT_SLUG = "rival-polymarket-learning"


def _notice(asset: str, horizon: int) -> SimpleNamespace:
    return SimpleNamespace(asset=asset, horizon=horizon, slug=f"{asset}-{horizon}")


class _StubResearch:
    """Stand-in for PolymarketResearch: only schedule/maybe_settle matter here."""

    def __init__(self, schedule) -> None:
        self._schedule = schedule
        self.settled = 0

    def schedule_study(self, notice, *, limit=2, cache_call=False):
        return self._schedule(notice)

    def maybe_settle(self, _polymarket) -> None:
        self.settled += 1


async def _never() -> bool:
    """A study that stays alive past the loop's 8 s wait budget."""
    await asyncio.Event().wait()
    return True


async def _done() -> bool:
    return True


def _client(notices, schedule) -> RivalDiscordClient:
    client = object.__new__(RivalDiscordClient)
    client.settings = SimpleNamespace(rival_polybolt_enabled=False)
    client.polybolt = None

    async def active():
        return list(notices)

    client.auto_hunt = SimpleNamespace(polymarket=SimpleNamespace(active=active))
    client.polymarket_research = _StubResearch(schedule)

    async def wait_until_ready() -> None:
        return None

    client.wait_until_ready = wait_until_ready
    # First check lets one pass run; the second stops the loop after it.
    closed = iter([False, True])
    client.is_closed = lambda: next(closed)
    return client


async def _run_one_pass(monkeypatch, client) -> list[str]:
    beats: list[str] = []
    monkeypatch.setattr(bot_module, "heartbeat", lambda slug: beats.append(slug))

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await client._poly_research_loop()
    return beats


async def test_heartbeat_fires_when_all_studies_time_out_or_are_cap_rejected(monkeypatch):
    """Two studies blocked >8 s + one cap-rejected study: zero completions,
    but the loop is alive, so the liveness check-in must still fire."""
    notices = [_notice("BTC", 5), _notice("ETH", 5), _notice("SOL", 5)]
    blocked: list[asyncio.Task] = []

    def schedule(notice):
        if notice.asset == "SOL":
            return None  # Both worker slots busy: cap-rejected.
        task = asyncio.create_task(_never())
        blocked.append(task)
        return task

    client = _client(notices, schedule)
    try:
        beats = await _run_one_pass(monkeypatch, client)
    finally:
        # The timed-out studies stay alive in production (_learning_tasks);
        # here the test owns them, so cancel them to keep the suite quiet.
        for task in blocked:
            task.cancel()
        if blocked:
            await asyncio.gather(*blocked, return_exceptions=True)

    assert beats == [HEARTBEAT_SLUG]
    assert client.polymarket_research.settled == 1


async def test_heartbeat_fires_when_studies_complete(monkeypatch):
    """The success path must keep checking in exactly once per pass."""
    notices = [_notice("BTC", 5)]

    def schedule(_notice):
        return asyncio.create_task(_done())

    client = _client(notices, schedule)
    beats = await _run_one_pass(monkeypatch, client)

    assert beats == [HEARTBEAT_SLUG]


async def test_heartbeat_fires_on_empty_listing(monkeypatch):
    """No notices at all is a completed (empty) pass, not a dead loop."""
    client = _client([], lambda _notice: None)
    beats = await _run_one_pass(monkeypatch, client)

    assert beats == [HEARTBEAT_SLUG]
