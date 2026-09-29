from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class HistoryArchiveBatch:
    batch_id: str
    provider: str
    bookmaker: str
    sport: str
    partition_date: date
    row_ids: tuple[int, ...]
    rows: tuple[dict, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class ArchiveArtifact:
    path: Path
    file_name: str
    size_bytes: int
    sha256: str
    md5: str
    row_count: int
    schema_version: int = 1


@dataclass(frozen=True)
class DriveObject:
    file_id: str
    name: str
    size_bytes: int | None
    md5: str | None
    parents: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveCompletion:
    batch_id: str
    history_file_id: str
    manifest_file_id: str
    completed_at: datetime
