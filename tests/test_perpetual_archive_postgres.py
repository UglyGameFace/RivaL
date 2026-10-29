"""Exercise copy-only candle receipts with the CI PostgreSQL 16 service."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.archive.perpetual_candles import PerpetualColdArchiveService
from parlay_bot.archive.writer import ParquetArchiveWriter
from parlay_bot.storage.hot import PostgresHotStore
from tests.test_perpetual_cold_archive import _Drive, _insert


@pytest.mark.asyncio
async def test_postgres_archive_receipts_survive_reopen_and_keep_hot_rows(tmp_path):
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("CI PostgreSQL service is not available")
    # Isolate this integration check from other PostgreSQL research suites.
    # The production backend still uses the real psycopg adapter and SQL.
    schema = "rival_perp_cold_archive_ci"
    admin = PostgresHotStore(dsn)
    with admin.connect() as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    separator = "&" if "?" in dsn else "?"
    isolated_dsn = f"{dsn}{separator}options=-csearch_path%3D{schema}"
    store = PostgresHotStore(isolated_dsn)
    store.initialize()
    drive = _Drive()
    service = PerpetualColdArchiveService(
        store=store, drive=drive,
        writer=ParquetArchiveWriter(tmp_path / "cold"),
    )
    service.initialize()
    now = datetime(2026, 10, 28, 20, tzinfo=UTC)
    moment = now - timedelta(days=45)
    # Use a unique test-only contract so this suite never depends on or
    # edits other test tables or cross-module data.
    with store.connect() as conn:
        conn.execute(
            "DELETE FROM perp_candle_archive_receipts WHERE venue=? AND contract=?",
            ("archive-ci", "BTCUSDT"),
        )
        conn.execute(
            "DELETE FROM perp_market_candles WHERE venue=? AND contract=?",
            ("archive-ci", "BTCUSDT"),
        )
    try:
        _insert(store, opened=moment, venue="archive-ci")
        result = await service.archive_ready(
            now=now, keep_hot_days=14, max_rows=100, max_batches=2
        )
        assert len(result) == 1
        with store.connect() as conn:
            receipt = conn.execute(
                "SELECT batch_id FROM perp_candle_archive_receipts "
                "WHERE venue=? AND contract=?",
                ("archive-ci", "BTCUSDT"),
            ).fetchone()
            assert receipt["batch_id"] == result[0].batch_id
            still_hot = conn.execute(
                "SELECT opened_at,first_observed_at FROM perp_market_candles "
                "WHERE venue=? AND contract=?",
                ("archive-ci", "BTCUSDT"),
            ).fetchone()
            assert still_hot["opened_at"] == moment.isoformat()
            assert still_hot["first_observed_at"] == (
                moment + timedelta(minutes=5, seconds=1)
            ).isoformat()
        reloaded = PerpetualColdArchiveService(
            store=store, drive=drive,
            writer=ParquetArchiveWriter(tmp_path / "cold"),
        )
        assert await reloaded.archive_ready(
            now=now, keep_hot_days=14, max_rows=100, max_batches=1
        ) == []
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_candle_archive_receipts WHERE venue=? AND contract=?",
                ("archive-ci", "BTCUSDT"),
            )
            conn.execute(
                "DELETE FROM perp_market_candles WHERE venue=? AND contract=?",
                ("archive-ci", "BTCUSDT"),
            )
        store.close()
        with admin.connect() as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()
