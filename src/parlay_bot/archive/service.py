from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from parlay_bot.archive.drive import GoogleDriveArchiveClient
from parlay_bot.archive.models import ArchiveCompletion
from parlay_bot.archive.queue import SQLiteArchiveQueue
from parlay_bot.archive.writer import ParquetArchiveWriter


class ColdArchiveService:
    """Commit closed SQLite line states to Drive only after verified data + manifest."""

    def __init__(
        self,
        *,
        queue: SQLiteArchiveQueue,
        writer: ParquetArchiveWriter,
        drive: GoogleDriveArchiveClient,
    ) -> None:
        self.queue = queue
        self.writer = writer
        self.drive = drive

    async def archive_one(
        self,
        *,
        cutoff: datetime,
        max_rows: int,
    ) -> ArchiveCompletion | None:
        batch = self.queue.next_batch(cutoff=cutoff, max_rows=max_rows)
        if batch is None:
            return None

        history = await asyncio.to_thread(self.writer.write_history, batch)
        manifest = None
        try:
            history_obj = await self.drive.ensure_history_file(
                artifact=history,
                batch_id=batch.batch_id,
            )
            self.queue.set_history_file(batch.batch_id, history_obj.file_id)

            manifest = await asyncio.to_thread(
                self.writer.write_manifest,
                batch=batch,
                history=history,
                history_drive_file_id=history_obj.file_id,
            )
            manifest_obj = await self.drive.ensure_manifest_file(
                artifact=manifest,
                batch_id=batch.batch_id,
            )
            self.queue.set_manifest_file(batch.batch_id, manifest_obj.file_id)

            completed_at = datetime.now(UTC)
            self.queue.mark_complete(
                batch_id=batch.batch_id,
                row_ids=batch.row_ids,
                completed_at=completed_at,
            )
            return ArchiveCompletion(
                batch_id=batch.batch_id,
                history_file_id=history_obj.file_id,
                manifest_file_id=manifest_obj.file_id,
                completed_at=completed_at,
            )
        finally:
            if self.queue.batch_record(batch.batch_id)?.get("status") == "complete":
                history.path.unlink(missing_ok=True)
                if manifest is not None:
                    manifest.path.unlink(missing_ok=True)

    async def archive_ready(
        self,
        *,
        cutoff: datetime,
        max_rows: int,
        max_batches: int = 4,
    ) -> list[ArchiveCompletion]:
        if max_batches < 1:
            raise ValueError("max_batches must be positive")

        completed: list[ArchiveCompletion] = []
        for _ in range(max_batches):
            result = await self.archive_one(cutoff=cutoff, max_rows=max_rows)
            if result is None:
                break
            completed.append(result)
        return completed
