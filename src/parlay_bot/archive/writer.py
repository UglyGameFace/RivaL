from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.archive.models import ArchiveArtifact, HistoryArchiveBatch

_SCHEMA_VERSION = 1
_COLUMNS = (
    "id",
    "selection_key",
    "provider",
    "fixture_id",
    "bookmaker",
    "sport_name",
    "tournament_name",
    "market_id",
    "market_name",
    "outcome_id",
    "outcome_name",
    "player_id",
    "player_name",
    "line_value",
    "line_group_value",
    "deeplink",
    "active",
    "main_line",
    "price_decimal",
    "price_american",
    "price_fractional",
    "bet_limit",
    "provider_changed_at",
    "bookmaker_changed_at",
    "exchange_meta_json",
    "state_fingerprint",
    "first_seen",
    "last_seen",
    "occurrences",
)


def _safe_slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    return "-".join(part for part in cleaned.split("-") if part) or "unknown"


def _digests(path: Path) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()


def _artifact(path: Path, *, row_count: int) -> ArchiveArtifact:
    sha256, md5 = _digests(path)
    return ArchiveArtifact(
        path=path,
        file_name=path.name,
        size_bytes=path.stat().st_size,
        sha256=sha256,
        md5=md5,
        row_count=row_count,
        schema_version=_SCHEMA_VERSION,
    )


class ParquetArchiveWriter:
    """Write explicit-schema ZSTD Parquet without adding DuckDB to the hot bot path."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    @staticmethod
    def _duckdb():
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Parquet archival requires the optional 'archive' dependency"
            ) from exc
        return duckdb

    @staticmethod
    def _row_tuple(row: dict) -> tuple:
        return tuple(row.get(column) for column in _COLUMNS)

    def write_history(self, batch: HistoryArchiveBatch) -> ArchiveArtifact:
        if not batch.rows:
            raise ValueError("cannot archive an empty batch")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        file_name = (
            f"sport={_safe_slug(batch.sport)}__date={batch.partition_date.isoformat()}"
            f"__book={_safe_slug(batch.bookmaker)}__provider={_safe_slug(batch.provider)}"
            f"__batch={batch.batch_id}.parquet"
        )
        output = self.output_dir / file_name
        temporary = output.with_suffix(".parquet.tmp")

        duckdb = self._duckdb()
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute("SET threads = 1")
            connection.execute("SET memory_limit = '128MB'")
            connection.execute(
                """
                CREATE TABLE history (
                    id BIGINT,
                    selection_key VARCHAR,
                    provider VARCHAR,
                    fixture_id VARCHAR,
                    bookmaker VARCHAR,
                    sport_name VARCHAR,
                    tournament_name VARCHAR,
                    market_id VARCHAR,
                    market_name VARCHAR,
                    outcome_id VARCHAR,
                    outcome_name VARCHAR,
                    player_id VARCHAR,
                    player_name VARCHAR,
                    line_value DOUBLE,
                    line_group_value DOUBLE,
                    deeplink VARCHAR,
                    active BOOLEAN,
                    main_line BOOLEAN,
                    price_decimal DOUBLE,
                    price_american VARCHAR,
                    price_fractional VARCHAR,
                    bet_limit DOUBLE,
                    provider_changed_at VARCHAR,
                    bookmaker_changed_at VARCHAR,
                    exchange_meta_json VARCHAR,
                    state_fingerprint VARCHAR,
                    first_seen VARCHAR,
                    last_seen VARCHAR,
                    occurrences BIGINT
                )
                """
            )
            placeholders = ",".join("?" for _ in _COLUMNS)
            connection.executemany(
                f"INSERT INTO history VALUES ({placeholders})",
                [self._row_tuple(row) for row in batch.rows],
            )
            escaped = str(temporary).replace("'", "''")
            connection.execute(
                f"COPY history TO '{escaped}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            connection.close()

        temporary.replace(output)
        artifact = _artifact(output, row_count=batch.row_count)
        if artifact.size_bytes <= 0:
            raise RuntimeError("Parquet writer produced an empty file")
        return artifact

    def write_manifest(
        self,
        *,
        batch: HistoryArchiveBatch,
        history: ArchiveArtifact,
        history_drive_file_id: str,
    ) -> ArchiveArtifact:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        row_id_hash = hashlib.sha256(
            ",".join(str(item) for item in batch.row_ids).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "batch_id": batch.batch_id,
            "created_at": datetime.now(UTC).isoformat(),
            "provider": batch.provider,
            "bookmaker": batch.bookmaker,
            "sport": batch.sport,
            "partition_date": batch.partition_date.isoformat(),
            "row_count": batch.row_count,
            "source": {
                "first_row_id": min(batch.row_ids),
                "last_row_id": max(batch.row_ids),
                "row_ids_sha256": row_id_hash,
                "first_seen": min(str(row["first_seen"]) for row in batch.rows),
                "last_seen": max(str(row["last_seen"]) for row in batch.rows),
            },
            "history_artifact": {
                "file_name": history.file_name,
                "size_bytes": history.size_bytes,
                "sha256": history.sha256,
                "md5": history.md5,
                "drive_file_id": history_drive_file_id,
                "format": "parquet",
                "compression": "zstd",
            },
        }
        path = self.output_dir / f"manifest__batch={batch.batch_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return _artifact(path, row_count=batch.row_count)
