"""Regression test: the price-formula loop must check in on liveness,
not on considered notices (RIVAL-8).

The Sentry cron monitor ``rival-formula-test`` failed while the loop was
alive: ``heartbeat()`` only fired inside ``if considered``, so a pass where
the Polymarket ``active()`` listing returned no notices sent no check-in
even though the loop itself was healthy.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from parlay_bot.discord_app import bot as bot_module
from parlay_bot.discord_app.bot import RivalDiscordClient

HEARTBEAT_SLUG = "rival-formula-test"


def _notice(asset: str, horizon: int) -> SimpleNamespace:
    return SimpleNamespace(
        asset=asset, horizon=horizon, issued=datetime.now(UTC),
    )


class _StubSpotReference:
    """Stand-in for SpotReference: prepare/snapshots are inert here."""

    async def prepare(self, _assets) -> None:
        return None

    async def prepare_native(self, _assets) -> None:
        return None

    async def snapshots(self, _assets) -> dict:
        return {}

    async def native_snapshots(self, _assets) -> dict:
        return {}


class _StubFormulaTest:
    """Stand-in for the formula test: only consider() calls matter here."""

    def __init__(self) -> None:
        self.considered = 0

    def consider(self, _notice, *, reference=None, learner=None) -> None:
        self.considered += 1


def _client(notices) -> RivalDiscordClient:
    client = object.__new__(RivalDiscordClient)

    async def active():
        return list(notices)

    client.auto_hunt = SimpleNamespace(polymarket=SimpleNamespace(active=active))
    client.spot_reference = _StubSpotReference()
    client.formula_test = _StubFormulaTest()
    client.polymarket_research = SimpleNamespace(learner=object())

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
    await client._formula_loop()
    return beats


async def test_heartbeat_fires_on_empty_listing(monkeypatch):
    """No notices at all is a completed (empty) pass, not a dead loop."""
    client = _client([])
    beats = await _run_one_pass(monkeypatch, client)

    assert beats == [HEARTBEAT_SLUG]
    assert client.formula_test.considered == 0


async def test_heartbeat_fires_when_notices_considered(monkeypatch):
    """The success path must keep checking in exactly once per pass."""
    client = _client([_notice("BTC", 5)])
    beats = await _run_one_pass(monkeypatch, client)

    assert beats == [HEARTBEAT_SLUG]
    assert client.formula_test.considered == 1
