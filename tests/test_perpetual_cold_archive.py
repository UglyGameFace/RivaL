"""Real SQLite/Parquet integration for copy-first perpetual market archiving."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from parlay_bot.archive.models import DriveObject
from parlay_bot.archive.perpetual_candles import PerpetualColdArchiveService
from parlay_bot.archive.writer import ParquetArchiveWriter
from parlay_bot.storage.hot import SQLiteHotStore


class _Drive:
    def __init__(self, fail_manifest=False):
        self.history = []
        self.manifests = []
        self.fail_manifest = fail_manifest

    async def ensure_history_file(self, *, artifact, batch_id):
        self.history.append((batch_id, artifact.size_bytes, artifact.md5))
        assert artifact.file_name.startswith("perp_candles__")
        return DriveObject(
            file_id=f"history-{batch_id}", name=artifact.file_name,
            size_bytes=artifact.size_bytes, md5=artifact.md5, parents=("history",),
        )

    async def ensure_manifest_file(self, *, artifact, batch_id):
        if self.fail_manifest:
            raise RuntimeError("manifest verification unavailable")
        manifest = json.loads(artifact.path.read_text())
        assert manifest["history"]["drive_file_id"] == f"history-{batch_id}"
        assert manifest["history"]["sha256"]
        assert manifest["archive_policy"] == "copy_only_no_hot_deletions"
        self.manifests.append(manifest)
        return DriveObject(
            file_id=f"manifest-{batch_id}", name=artifact.file_name,
            size_bytes=artifact.size_bytes, md5=artifact.md5, parents=("manifests",),
        )


def _insert(store, *, opened, value=100.0, venue="gate"):
    closed = opened + timedelta(minutes=5)
    observed = closed + timedelta(seconds=1)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles
            (venue,contract,quote,interval_minutes,opened_at,closed_at,
             first_observed_at,open_price,high_price,low_price,close_price,
             volume_base,trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, "BTCUSDT", "USDT", 5,
                opened.isoformat(), closed.isoformat(), observed.isoformat(),
                value, value+1, value-1, value+0.5, 1.25, 7,
            ),
        )


def _count(store, table):
    with store.connect() as conn:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _setup(tmp_path):
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    store.initialize()
    drive = _Drive()
    service = PerpetualColdArchiveService(
        store=store, drive=drive,
        writer=ParquetArchiveWriter(tmp_path / "archive"),
    )
    service.initialize()
    return store, drive, service


@pytest.mark.asyncio
async def test_archive_old_bars_not_active_hot_candles_and_restarts(tmp_path):
    store, drive, service = _setup(tmp_path)
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    old = now - timedelta(days=30)
    _insert(store, opened=old)
    _insert(store, opened=old + timedelta(minutes=5), value=110)
    _insert(store, opened=now - timedelta(hours=1), value=150)
    first = await service.archive_ready(
        now=now, keep_hot_days=14, max_rows=1000, max_batches=2,
    )
    assert len(first) == 1
    assert _count(store, "perp_candle_archive_receipts") == 2
    assert _count(store, "perp_market_candles") == 3  # NO hot deletions
    assert drive.manifests[0]["row_count"] == 2
    assert drive.manifests[0]["dataset"] == "perp_market_candles"
    assert len(drive.history) == 1

    restarted = PerpetualColdArchiveService(
        store=store, drive=drive,
        writer=ParquetArchiveWriter(tmp_path / "archive"),
    )
    assert await restarted.archive_ready(
        now=now, keep_hot_days=14, max_rows=1000, max_batches=1,
    ) == []
    assert len(drive.history) == 1
    assert list((tmp_path / "archive").iterdir()) == []


@pytest.mark.asyncio
async def test_backfilled_older_day_is_not_lost_behind_completed_cursor(tmp_path):
    store, drive, service = _setup(tmp_path)
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    _insert(store, opened=now - timedelta(days=20))
    await service.archive_ready(
        now=now, keep_hot_days=14, max_rows=10, max_batches=1
    )
    _insert(store, opened=now - timedelta(days=40), value=130, venue="okx")
    newer = await service.archive_ready(
        now=now, keep_hot_days=14, max_rows=10, max_batches=1
    )
    assert len(newer) == 1
    assert drive.manifests[-1]["venue"] == "okx"
    assert _count(store, "perp_candle_archive_receipts") == 2


@pytest.mark.asyncio
async def test_manifest_failure_leaves_candles_hot_and_unmarked(tmp_path):
    store, drive, service = _setup(tmp_path)
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    _insert(store, opened=now - timedelta(days=30))
    drive.fail_manifest = True
    with pytest.raises(RuntimeError, match="manifest verification"):
        await service.archive_ready(
            now=now, keep_hot_days=14, max_rows=10, max_batches=1
        )
    assert _count(store, "perp_candle_archive_receipts") == 0
    assert _count(store, "perp_market_candles") == 1
    assert list((tmp_path / "archive").iterdir())  # keep recovery evidence
    drive.fail_manifest = False
    completed = await service.archive_ready(
        now=now, keep_hot_days=14, max_rows=10, max_batches=1
    )
    assert len(completed) == 1
    assert _count(store, "perp_candle_archive_receipts") == 1


@pytest.mark.asyncio
async def test_real_parquet_roundtrip_preserves_observation_provenance(tmp_path):
    store, _drive, service = _setup(tmp_path)
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    source = now - timedelta(days=30)
    _insert(store, opened=source)
    batch = service._next_batch(
        cutoff=now - timedelta(days=14), max_rows=10
    )
    assert batch is not None
    artifact, _ = service._write_files(batch)
    db = duckdb.connect(":memory:")
    try:
        rows = db.execute(
            "SELECT venue,opened_at,first_observed_at,close_price,trades "
            "FROM read_parquet(?)", [str(artifact.path)]
        ).fetchall()
    finally:
        db.close()
    assert rows == [(
        "gate", source.isoformat(),
        (source + timedelta(minutes=5, seconds=1)).isoformat(),
        100.5, 7,
    )]
    assert artifact.size_bytes > 0
    assert len(artifact.sha256) == 64


def test_config_rejects_unconnected_archive_and_bounds(tmp_path):
    from pydantic import ValidationError

    from parlay_bot.archive.runtime import build_cold_archive_runtime
    from parlay_bot.config import Settings

    settings = Settings(
        _env_file=None,
        rival_perp_archive_enabled=True,
        rival_archive_enabled=False,
    )
    with pytest.raises(RuntimeError, match="requires RIVAL_ARCHIVE_ENABLED"):
        build_cold_archive_runtime(settings)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rival_perp_archive_keep_hot_days=1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rival_perp_archive_batch_rows=9999)


def test_archive_receipts_use_bounded_queries_for_large_batch(tmp_path, monkeypatch):
    from parlay_bot.storage import database as db_module

    store, _drive, service = _setup(tmp_path)
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    anchor = now - timedelta(days=30)
    with store.connect() as conn:
        for index in range(235):
            opened = anchor + timedelta(minutes=5 * index)
            closed = opened + timedelta(minutes=5)
            conn.execute(
                "INSERT INTO perp_market_candles "
                "(venue,contract,quote,interval_minutes,opened_at,closed_at,"
                "first_observed_at,open_price,high_price,low_price,close_price,"
                "volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("gate", "BTCUSDT", "USDT", 5, opened.isoformat(),
                 closed.isoformat(), (closed+timedelta(seconds=1)).isoformat(),
                 100.0, 101.0, 99.0, 100.5, 1.25, 7),
            )
    batch = service._next_batch(
        cutoff=now-timedelta(days=14), max_rows=235,
    )
    assert batch is not None and len(batch.rows) == 235
    original = db_module._Connection.execute
    statements = []

    def counted(self, sql, params=()):
        if "perp_candle_archive_receipts" in sql and sql.lstrip().upper().startswith("INSERT"):
            statements.append("insert")
        if "SELECT venue,contract,quote,interval_minutes" in sql:
            statements.append("read")
        return original(self, sql, params)

    monkeypatch.setattr(db_module._Connection, "execute", counted)
    service._mark_archived(batch, now)
    assert statements.count("insert") == 3
    assert statements.count("read") == 3
    assert _count(store, "perp_candle_archive_receipts") == 235
    assert _count(store, "perp_market_candles") == 235


def test_archive_receipts_roll_back_all_batches_when_late_source_changes(tmp_path):
    store, _drive, service = _setup(tmp_path)
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    anchor = now - timedelta(days=30)
    with store.connect() as conn:
        for index in range(120):
            opened = anchor + timedelta(minutes=5 * index)
            closed = opened + timedelta(minutes=5)
            conn.execute(
                "INSERT INTO perp_market_candles "
                "(venue,contract,quote,interval_minutes,opened_at,closed_at,"
                "first_observed_at,open_price,high_price,low_price,close_price,"
                "volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("gate", "BTCUSDT", "USDT", 5, opened.isoformat(),
                 closed.isoformat(), (closed+timedelta(seconds=1)).isoformat(),
                 100.0, 101.0, 99.0, 100.5, 1.25, 7),
            )
    batch = service._next_batch(
        cutoff=now-timedelta(days=14), max_rows=120,
    )
    assert batch is not None and len(batch.rows) == 120
    with store.connect() as conn:
        conn.execute(
            "UPDATE perp_market_candles SET volume_base=? WHERE venue=? "
            "AND contract=? AND opened_at=?",
            (2.5, "gate", "BTCUSDT", batch.rows[105]["opened_at"]),
        )
    with pytest.raises(RuntimeError, match="source changed"):
        service._mark_archived(batch, now)
    assert _count(store, "perp_candle_archive_receipts") == 0
    assert _count(store, "perp_market_candles") == 120
