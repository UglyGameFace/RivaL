"""Copy-only, bounded Google Drive warehouse for immutable perpetual candles.

Backfilled records are selected by an unarchived composite key, never by a
monotonic timestamp cursor: older imports remain eligible after newer batches.
Marking a receipt cannot delete or reclassify a research observation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.archive.drive import GoogleDriveArchiveClient
from parlay_bot.archive.models import ArchiveArtifact, ArchiveCompletion
from parlay_bot.archive.writer import ParquetArchiveWriter
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import RelationalHotStore

_COLUMNS = (
    "venue", "contract", "quote", "interval_minutes", "opened_at", "closed_at",
    "first_observed_at", "open_price", "high_price", "low_price", "close_price",
    "volume_base", "trades",
)
_ID = ("venue", "contract", "quote", "interval_minutes", "opened_at")


def _artifact(path: Path, row_count: int) -> ArchiveArtifact:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=path.stat().st_size,
        sha256=sha.hexdigest(), md5=md5.hexdigest(), row_count=row_count,
    )


@dataclass(frozen=True)
class CandleArchiveBatch:
    batch_id: str
    rows: tuple[dict, ...]
    venue: str
    contract: str
    quote: str
    interval_minutes: int
    partition_date: str


class PerpetualColdArchiveService:
    """Append-only Drive archive with failure-atomic PostgreSQL receipts."""

    def __init__(
        self, *, store: RelationalHotStore, drive: GoogleDriveArchiveClient,
        writer: ParquetArchiveWriter,
    ) -> None:
        self.store = store
        self.drive = drive
        self.writer = writer
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        PerpetualMarketStore(self.store).initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_candle_archive_receipts (
                    venue TEXT NOT NULL, contract TEXT NOT NULL,
                    quote TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
                    opened_at TEXT NOT NULL, batch_id TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY (venue,contract,quote,interval_minutes,opened_at)
                );
                CREATE INDEX IF NOT EXISTS idx_perp_archive_cold_candidates
                    ON perp_market_candles (closed_at,venue,contract,quote,interval_minutes,opened_at);
                """
            )
        self._initialized = True

    def _next_batch(self, *, cutoff: datetime, max_rows: int) -> CandleArchiveBatch | None:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        if not 1 <= max_rows <= 5000:
            raise ValueError("perpetual archive max_rows must be 1..5000")
        self.initialize()
        with self.store.connect() as conn:
            first = conn.execute(
                """
                SELECT c.venue,c.contract,c.quote,c.interval_minutes,
                       substr(c.opened_at,1,10) AS partition_date
                FROM perp_market_candles c
                LEFT JOIN perp_candle_archive_receipts a
                  ON a.venue=c.venue AND a.contract=c.contract
                 AND a.quote=c.quote AND a.interval_minutes=c.interval_minutes
                 AND a.opened_at=c.opened_at
                WHERE a.opened_at IS NULL AND c.closed_at < ?
                ORDER BY c.closed_at,c.venue,c.contract,c.opened_at LIMIT 1
                """,
                (cutoff.astimezone(UTC).isoformat(),),
            ).fetchone()
            if first is None:
                return None
            selected = conn.execute(
                """
                SELECT c.venue,c.contract,c.quote,c.interval_minutes,c.opened_at,
                       c.closed_at,c.first_observed_at,c.open_price,c.high_price,
                       c.low_price,c.close_price,c.volume_base,c.trades
                FROM perp_market_candles c
                LEFT JOIN perp_candle_archive_receipts a
                  ON a.venue=c.venue AND a.contract=c.contract
                 AND a.quote=c.quote AND a.interval_minutes=c.interval_minutes
                 AND a.opened_at=c.opened_at
                WHERE a.opened_at IS NULL AND c.closed_at < ?
                  AND c.venue=? AND c.contract=? AND c.quote=?
                  AND c.interval_minutes=? AND substr(c.opened_at,1,10)=?
                ORDER BY c.opened_at LIMIT ?
                """,
                (
                    cutoff.astimezone(UTC).isoformat(), first["venue"],
                    first["contract"], first["quote"], first["interval_minutes"],
                    first["partition_date"], max_rows,
                ),
            ).fetchall()
        rows = tuple(dict(row) for row in selected)
        if not rows:
            return None
        # Include every physical field in the ID: immutable source records
        # cannot be silently replaced during a crash/retry or a late backfill.
        serialized = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return CandleArchiveBatch(
            batch_id="perp-" + digest[:32],
            rows=rows, venue=str(first["venue"]), contract=str(first["contract"]),
            quote=str(first["quote"]), interval_minutes=int(first["interval_minutes"]),
            partition_date=str(first["partition_date"]),
        )

    def _write_files(self, batch: CandleArchiveBatch) -> tuple[ArchiveArtifact, Path]:
        import duckdb  # optional archive dependency; unavailable when disabled

        path = self.writer.output_dir
        path.mkdir(parents=True, exist_ok=True)
        name = f"perp_candles__{batch.partition_date}__{batch.batch_id}.parquet"
        dest = path / name
        temporary = path / f"{name}.tmp"
        db = duckdb.connect(":memory:")
        try:
            db.execute("SET threads = 1")
            db.execute("SET memory_limit = '128MB'")
            db.execute(
                """
                CREATE TABLE candles (
                    venue VARCHAR, contract VARCHAR, quote VARCHAR,
                    interval_minutes INTEGER, opened_at VARCHAR,
                    closed_at VARCHAR, first_observed_at VARCHAR,
                    open_price DOUBLE, high_price DOUBLE, low_price DOUBLE,
                    close_price DOUBLE, volume_base DOUBLE, trades INTEGER
                )
                """
            )
            db.executemany(
                "INSERT INTO candles VALUES (" + ",".join("?" for _ in _COLUMNS) + ")",
                [tuple(row[k] for k in _COLUMNS) for row in batch.rows],
            )
            escaped = str(temporary).replace("'", "''")
            db.execute(f"COPY candles TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            db.close()
        temporary.replace(dest)
        history = _artifact(dest, len(batch.rows))
        if history.size_bytes <= 0:
            raise RuntimeError("perpetual archive produced an empty data file")
        return history, dest

    def _write_manifest(
        self, batch: CandleArchiveBatch, data: ArchiveArtifact, file_id: str,
    ) -> ArchiveArtifact:
        payload = {
            "schema_version": 1, "dataset": "perp_market_candles",
            "archive_policy": "copy_only_no_hot_deletions",
            "batch_id": batch.batch_id, "venue": batch.venue,
            "contract": batch.contract, "quote": batch.quote,
            "interval_minutes": batch.interval_minutes,
            "partition_date": batch.partition_date,
            "row_count": len(batch.rows),
            "first_opened_at": batch.rows[0]["opened_at"],
            "last_opened_at": batch.rows[-1]["opened_at"],
            "history": {
                "file_name": data.file_name, "drive_file_id": file_id,
                "size_bytes": data.size_bytes, "sha256": data.sha256,
                "md5": data.md5, "format": "parquet", "compression": "zstd",
            },
        }
        output = self.writer.output_dir / f"manifest__{batch.batch_id}.json"
        temp = output.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", "utf-8")
        temp.replace(output)
        return _artifact(output, len(batch.rows))

    def _mark_archived(self, batch: CandleArchiveBatch, when: datetime) -> None:
        stamp = when.astimezone(UTC).isoformat()
        with self.store.connect() as conn:
            for row in batch.rows:
                # A simultaneous writer or correction must not certify an
                # unmatched export. Read the authoritative data before the
                # receipt; immutable ingest forbids updates, but verify anyway.
                current = conn.execute(
                    """
                    SELECT venue,contract,quote,interval_minutes,opened_at,
                           closed_at,first_observed_at,open_price,high_price,
                           low_price,close_price,volume_base,trades
                    FROM perp_market_candles
                    WHERE venue=? AND contract=? AND quote=?
                      AND interval_minutes=? AND opened_at=?
                    """,
                    tuple(row[k] for k in _ID),
                ).fetchone()
                if current is None or dict(current) != row:
                    raise RuntimeError("perpetual archive source changed; refusing receipt")
                result = conn.execute(
                    """
                    INSERT INTO perp_candle_archive_receipts
                      (venue,contract,quote,interval_minutes,opened_at,batch_id,archived_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT DO NOTHING
                    """,
                    (*tuple(row[k] for k in _ID), batch.batch_id, stamp),
                )
                if result.rowcount != 1:
                    raise RuntimeError("perpetual archive receipt collision; refusing commit")

    async def archive_ready(
        self, *, now: datetime, keep_hot_days: int,
        max_rows: int, max_batches: int,
    ) -> list[ArchiveCompletion]:
        if now.tzinfo is None or not 7 <= keep_hot_days <= 365:
            raise ValueError("perpetual archive requires UTC time and 7..365 hot days")
        if not 1 <= max_batches <= 4:
            raise ValueError("perpetual archive max_batches must be 1..4")
        cutoff = now.astimezone(UTC) - timedelta(days=keep_hot_days)
        results: list[ArchiveCompletion] = []
        for _ in range(max_batches):
            batch = await asyncio.to_thread(
                self._next_batch, cutoff=cutoff, max_rows=max_rows
            )
            if batch is None:
                break
            history, _ = await asyncio.to_thread(self._write_files, batch)
            manifest = None
            try:
                remote_history = await self.drive.ensure_history_file(
                    artifact=history, batch_id=batch.batch_id
                )
                manifest = await asyncio.to_thread(
                    self._write_manifest, batch, history, remote_history.file_id
                )
                remote_manifest = await self.drive.ensure_manifest_file(
                    artifact=manifest, batch_id=batch.batch_id
                )
                when = datetime.now(UTC)
                await asyncio.to_thread(self._mark_archived, batch, when)
                results.append(ArchiveCompletion(
                    batch_id=batch.batch_id, history_file_id=remote_history.file_id,
                    manifest_file_id=remote_manifest.file_id, completed_at=when,
                ))
            finally:
                # Keep local files on ANY failed stage as recovery evidence.
                # Only verified and committed data is safe to discard locally.
                if len(results) and results[-1].batch_id == batch.batch_id:
                    history.path.unlink(missing_ok=True)
                    if manifest is not None:
                        manifest.path.unlink(missing_ok=True)
        return results
