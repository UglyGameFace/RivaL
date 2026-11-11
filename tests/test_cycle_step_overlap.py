"""A cycle step that outlives its wait is never started a second time on top."""
from __future__ import annotations

import asyncio
import threading

import pytest

from parlay_bot.perpetuals.autonomous import AutonomousResearch, StepStillRunning


def test_a_slow_step_is_skipped_until_its_last_run_finishes():
    research = object.__new__(AutonomousResearch)
    release = threading.Event()
    calls = []

    def slow():
        calls.append("run")
        release.wait(5)
        return "done"

    async def scenario():
        with pytest.raises(TimeoutError):
            await research._in_thread("forward.run_once", slow, timeout=0.05)
        # Next cycle: the first run is still going, so no second copy starts.
        with pytest.raises(StepStillRunning):
            await research._in_thread("forward.run_once", slow, timeout=0.05)
        assert calls == ["run"]
        release.set()
        await asyncio.sleep(0.1)
        # Once it has finished, the step runs normally again.
        assert await research._in_thread("forward.run_once", slow, timeout=1) == "done"
        assert calls == ["run", "run"]

    asyncio.run(scenario())


def test_a_still_running_step_counts_as_a_timeout_for_existing_handlers():
    assert issubclass(StepStillRunning, TimeoutError)


def test_different_steps_do_not_block_each_other():
    research = object.__new__(AutonomousResearch)
    release = threading.Event()

    async def scenario():
        with pytest.raises(TimeoutError):
            await research._in_thread("multi.settle", lambda: release.wait(5), timeout=0.05)
        assert await research._in_thread("multi.issue", lambda: 3, timeout=1) == 3
        release.set()

    asyncio.run(scenario())
