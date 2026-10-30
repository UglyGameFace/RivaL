"""Sports forecast jobs are bounded, deduped, and shed loudly.

Every fetched event used to spawn an unbounded thread job repeating a
chronological backtest. The collector now caps concurrency at 4 and
in-flight jobs at 8, skips duplicates already in flight, and logs a
distinct outcome for each job instead of silently queueing.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest

from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.storage.hot import SQLiteHotStore


class StubResults:
    def __init__(self):
        self.current = 0
        self.peak = 0
        self.lock = threading.Lock()

    def issue_forecast(self, event, *, observed_at):
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        try:
            time.sleep(0.05)
        finally:
            with self.lock:
                self.current -= 1
        return "issued"


def make_collector(tmp_path):
    store = SQLiteHotStore(tmp_path / "collector.sqlite")
    collector = CurrentBoardCollector(client=object(), store=store)
    return collector, store


@pytest.mark.asyncio
async def test_forecast_concurrency_capped_and_overflow_shed(tmp_path):
    collector, store = make_collector(tmp_path)
    results = StubResults()
    try:
        tasks = [
            asyncio.create_task(collector._run_forecast(
                results, {"eventID": f"ev-{i}"},
                observed_at=datetime.now(UTC),
            ))
            for i in range(10)
        ]
        outcomes = await asyncio.gather(*tasks)
        assert results.peak <= 4
        assert outcomes.count("shed") == 2
        assert outcomes.count("issued") == 8
        assert collector._forecast_inflight == set()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_duplicate_inflight_event_is_shed(tmp_path):
    collector, store = make_collector(tmp_path)
    results = StubResults()
    try:
        first = asyncio.create_task(collector._run_forecast(
            results, {"eventID": "dup"},
            observed_at=datetime.now(UTC),
        ))
        await asyncio.sleep(0)
        second = await collector._run_forecast(
            results, {"eventID": "dup"},
            observed_at=datetime.now(UTC),
        )
        assert second == "shed"
        assert await first == "issued"
    finally:
        store.close()
