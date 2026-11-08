"""Regression test: the Intelligence review loop must check in on liveness,
not on save success (RIVAL-B, defect 1).

The Sentry cron monitor ``rival-intelligence-reviews`` paged while the loop
was alive: ``heartbeat()`` only fired inside ``if saved``, so a review that
completed but landed past the slot+5min save window (saved=0) sent no
check-in even though the loop itself was healthy. A completed review proves
the loop is alive; the landing signal stays in the ``saved=%d`` log line
and the hourly tally. Skips and failures must still not check in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from parlay_bot.discord_app import bot as bot_module
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals import intelligence as intelligence_module
from parlay_bot.perpetuals.ai_types import ResearchUnavailable
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence

HEARTBEAT_SLUG = "rival-intelligence-reviews"


def _ledger_factory(saved: int):
    """Stand-in for IntelligenceLedger: record returns a fixed saved count."""

    class _StubLedger:
        def __init__(self, _store) -> None:
            pass

        def record(self, **_kwargs) -> int:
            return saved

    return _StubLedger


class _StubResearch:
    """Stand-in for the Intelligence researcher."""

    def __init__(self, review=None, error: Exception | None = None) -> None:
        self._review = review if review is not None else {"model": "test-model"}
        self._error = error

    async def explain_models(self, **_kwargs):
        if self._error is not None:
            raise self._error
        return self._review


def _client(research, saved: int) -> RivalDiscordClient:
    client = object.__new__(RivalDiscordClient)
    client.settings = SimpleNamespace(rival_llm_background_opt_in=True)
    client.store = SimpleNamespace()
    client.perp_collector = SimpleNamespace(
        assets=("BTC",), market=SimpleNamespace(), providers=("binance",),
    )
    client.intelligence_research = research

    async def wait_until_ready() -> None:
        return None

    client.wait_until_ready = wait_until_ready
    # First check lets one slot run; the second stops the loop after it.
    closed = iter([False, True])
    client.is_closed = lambda: next(closed)
    return client


async def _run_one_slot(monkeypatch, client, saved: int) -> list[str]:
    beats: list[str] = []
    monkeypatch.setattr(bot_module, "heartbeat", lambda slug: beats.append(slug))
    monkeypatch.setattr(
        intelligence_module, "IntelligenceLedger", _ledger_factory(saved))
    monkeypatch.setattr(intelligence_module, "model_calls", lambda *a, **k: {})
    monkeypatch.setattr(intelligence_module, "market_context", lambda *a, **k: {})
    monkeypatch.setattr(
        bot_module.host_memory, "snapshot",
        lambda: SimpleNamespace(pressure=False))

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await client._intelligence_loop()
    return beats


async def test_heartbeat_fires_when_review_completes_with_saved_zero(monkeypatch):
    """The regression: a completed review that landed late (saved=0) is a
    live loop, so the liveness check-in must still fire."""
    client = _client(_StubResearch(), saved=0)
    beats = await _run_one_slot(monkeypatch, client, saved=0)

    assert beats == [HEARTBEAT_SLUG]


async def test_heartbeat_fires_when_review_completes_with_saved(monkeypatch):
    """The success path must keep checking in exactly once per slot."""
    client = _client(_StubResearch(), saved=1)
    beats = await _run_one_slot(monkeypatch, client, saved=1)

    assert beats == [HEARTBEAT_SLUG]


async def test_no_heartbeat_when_review_is_skipped(monkeypatch):
    """A skipped review (cap hit / thin evidence) is not a completed review:
    no spurious check-in."""
    for error in (
        ResearchUnavailable("budget exhausted"),
        InsufficientEvidence("fewer than three healthy venues"),
    ):
        client = _client(_StubResearch(error=error), saved=0)
        beats = await _run_one_slot(monkeypatch, client, saved=0)

        assert beats == []


async def test_no_heartbeat_when_review_fails(monkeypatch):
    """An unexpected failure inside the review is not a completed review:
    no spurious check-in."""
    client = _client(_StubResearch(error=RuntimeError("boom")), saved=0)
    beats = await _run_one_slot(monkeypatch, client, saved=0)

    assert beats == []
