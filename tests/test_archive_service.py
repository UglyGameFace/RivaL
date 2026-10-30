from __future__ import annotations

import asyncio
import itertools
import time
from datetime import timedelta

import pytest

from parlay_bot.archive.models import DriveObject
from parlay_bot.archive.queue import SQLiteArchiveQueue
from parlay_bot.archive.service import ColdArchiveService
from parlay_bot.archive.writer import ParquetArchiveWriter
from tests.test_archive_queue import _seed_closed_state


class FakeDrive:
    def __init__(self) -> None:
        self.history_calls = 0
        self.manifest_calls = 0

    async def ensure_history_file(self, *, artifact, batch_id):
        self.history_calls += 1
        assert artifact.file_name.endswith(".parquet")
        assert batch_id
        return DriveObject(
            file_id="history-drive-id",
            name=artifact.file_name,
            size_bytes=artifact.size_bytes,
            md5=artifact.md5,
            parents=("history",),
        )

    async def ensure_manifest_file(self, *, artifact, batch_id):
        self.manifest_calls += 1
        assert artifact.file_name.endswith(".json")
        assert batch_id
        return DriveObject(
            file_id="manifest-drive-id",
            name=artifact.file_name,
            size_bytes=artifact.size_bytes,
            md5=artifact.md5,
            parents=("manifests",),
        )


@pytest.mark.asyncio
async def test_service_commits_rows_only_after_both_artifacts_exist(tmp_path) -> None:
    store, now = _seed_closed_state(tmp_path / "rival.sqlite")
    queue = SQLiteArchiveQueue(store.path)
    writer = ParquetArchiveWriter(tmp_path / "archive")
    drive = FakeDrive()
    service = ColdArchiveService(queue=queue, writer=writer, drive=drive)

    result = await service.archive_one(
        cutoff=now - timedelta(hours=1),
        max_rows=100,
    )

    assert result is not None
    assert result.history_file_id == "history-drive-id"
    assert result.manifest_file_id == "manifest-drive-id"
    assert drive.history_calls == 1
    assert drive.manifest_calls == 1

    record = queue.batch_record(result.batch_id)
    assert record is not None
    assert record["status"] == "complete"

    with store.connect() as connection:
        rows = connection.execute(
            "SELECT archived_batch_id FROM odds_changes ORDER BY id"
        ).fetchall()
    assert rows[0]["archived_batch_id"] == result.batch_id
    assert rows[1]["archived_batch_id"] is None

    assert list((tmp_path / "archive").iterdir()) == []


@pytest.mark.asyncio
async def test_service_returns_none_when_no_closed_states_are_ready(tmp_path) -> None:
    store, now = _seed_closed_state(tmp_path / "rival.sqlite")
    queue = SQLiteArchiveQueue(store.path)
    writer = ParquetArchiveWriter(tmp_path / "archive")
    service = ColdArchiveService(queue=queue, writer=writer, drive=FakeDrive())

    result = await service.archive_one(
        cutoff=now - timedelta(hours=4),
        max_rows=100,
    )

    assert result is None


class _SlowQueue(SQLiteArchiveQueue):
    """Simulates the slow production GROUP BY: blocks its thread, not the loop."""

    def next_batch(self, *, cutoff, max_rows):
        time.sleep(1.0)
        return super().next_batch(cutoff=cutoff, max_rows=max_rows)


@pytest.mark.asyncio
async def test_archive_one_never_blocks_the_event_loop(tmp_path) -> None:
    """A slow archive query must not freeze Discord heartbeats/interactions.

    Regression test for the post-deploy heartbeat block: the cold archive
    ran its synchronous database calls on the event loop, stalling gateway
    heartbeats for 10+ seconds. A ticker task must keep running with no
    long stall while a deliberately slow next_batch executes.
    """
    store, now = _seed_closed_state(tmp_path / "rival.sqlite")
    queue = _SlowQueue(store.path)
    writer = ParquetArchiveWriter(tmp_path / "archive")
    service = ColdArchiveService(queue=queue, writer=writer, drive=FakeDrive())

    ticks: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        result = await service.archive_one(
            cutoff=now - timedelta(hours=1),
            max_rows=100,
        )
    finally:
        ended = time.monotonic()
        stop.set()
        await task
    assert result is not None
    marks = [started, *(t for t in ticks if started < t < ended), ended]
    longest_stall = max(b - a for a, b in itertools.pairwise(marks))
    assert longest_stall < 0.5, f"event loop stalled {longest_stall:.2f}s"
