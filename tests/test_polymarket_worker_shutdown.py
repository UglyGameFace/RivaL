"""Polymarket study workers must release DB work before pool shutdown."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from parlay_bot.perpetuals.polymarket_research import PolymarketResearch


@pytest.mark.asyncio
async def test_shutdown_waits_for_study_thread_after_coordinator_cancellation():
    research = PolymarketResearch(store=object())
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def study(_notice):
        entered.set()
        try:
            assert release.wait(timeout=5), "test worker was not released"
            return
        finally:
            finished.set()

    research.study = study
    task = research.schedule_study(SimpleNamespace(slug="test-contract"))
    assert task is not None
    assert await asyncio.to_thread(entered.wait, 3)
    closing = asyncio.create_task(research.aclose())
    await asyncio.sleep(0.05)
    assert not closing.done()
    assert not finished.is_set()
    release.set()
    await asyncio.wait_for(closing, timeout=3)
    assert finished.is_set()
    assert not research._study_workers
    assert not research._learning_tasks
