"""DKeX public reports are real research input, not live user quotes."""
from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime, timedelta

import pytest

from parlay_bot.research.dkex_reports import (
    DKeXPublicReports,
    parse_daily_report,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def snapshot(kind="settlement"):
    if kind == "settlement":
        return b"Market,Settlement Price\nDKEX-TEST-1,1.00\nDKEX-TEST-2,0.50\nDKEX-TEST-3,0.00\n"
    return b"Market,Volume\nDKEX-TEST-1,100\nDKEX-TEST-2,45\n"


def parsed(kind="settlement", *, blob=None, day=date(2026, 10, 28)):
    raw = blob if blob is not None else snapshot(kind)
    return parse_daily_report(
        raw, day=day, kind=kind,
        sha256=hashlib.sha256(raw).hexdigest(), symbol_column="Market",
        price_column="Settlement Price" if kind == "settlement" else None,
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_immutable_daily_settlement_evidence_and_voids(tmp_path, backend):
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if backend == "postgres" and not dsn:
        pytest.skip("PostgreSQL test DSN not configured")
    store = (
        PostgresHotStore(dsn) if backend == "postgres"
        else SQLiteHotStore(tmp_path / "dkex.sqlite")
    )
    monitor = DKeXPublicReports(store)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    try:
        monitor.initialize()
        # Keep each test isolated in PostgreSQL integration CI.
        if backend == "postgres":
            with store.connect() as conn:
                conn.execute("DELETE FROM rival_dkex_daily_contracts")
                conn.execute("DELETE FROM rival_dkex_public_reports")
        first = parsed()
        assert monitor.ingest(first, imported_at=now) == 3
        assert monitor.ingest(first, imported_at=now) == 0
        assert monitor.ingest(parsed("market"), imported_at=now) == 2
        report = monitor.summary()
        assert report["daily_reports"] == 2
        assert report["contract_rows"] == 5
        assert report["settled_contract_rows"] == 3
        assert report["half_value_settlements"] == 1
        assert report["latest_report_day"] == "2026-10-28"
        assert report["official_source_independently_verified"] is False
        assert report["live_quotes_connected"] is False
        assert report["contract_specific_forward_win_rate"] is None
        assert report["prediction_calls_enabled"] is False
        revised = parsed(blob=b"Market,Settlement Price\nDKEX-TEST-1,0.00\n")
        with pytest.raises(ValueError, match="conflicting"):
            monitor.ingest(revised, imported_at=now)
        assert monitor.summary()["settled_contract_rows"] == 3
        with pytest.raises(ValueError, match="future"):
            monitor.ingest(
                parsed(day=now.date()+timedelta(days=1)), imported_at=now,
            )
    finally:
        store.close()


def test_invalid_report_is_rejected_before_database_mutation():
    source = snapshot()
    sha = hashlib.sha256(source).hexdigest()
    with pytest.raises(ValueError, match="checksum"):
        parse_daily_report(
            source+b"bad", day=date(2026, 10, 28), kind="settlement",
            sha256=sha, symbol_column="Market", price_column="Settlement Price",
        )
    for data in (
        b"Market,Settlement Price\nSAME,1\nSAME,0\n",
        b"Market,Settlement Price\nINVALID,1.4\n",
        b"Market,Settlement Price\nINVALID,nan\n",
        b"Market,Settlement Price\nINVALID,-0.25\n",
        b"Market,Settlement Price\n,1\n",
    ):
        with pytest.raises(ValueError):
            parsed(blob=data)
    with pytest.raises(ValueError, match="column"):
        parse_daily_report(
            source, day=date(2026, 10, 28), kind="settlement",
            sha256=sha, symbol_column="Nonexistent", price_column="Settlement Price",
        )
    with pytest.raises(ValueError, match="type"):
        parse_daily_report(
            source, day=date(2026, 10, 28), kind="prices",
            sha256=sha, symbol_column="Market",
        )


def test_public_market_report_does_not_invent_executable_prices():
    report = parsed("market")
    assert len(report.rows) == 2
    assert all(price is None for _, price in report.rows)
    assert report.source_page.endswith("/daily-market-reports")


def test_dkex_report_command_dry_run_requires_no_trading_api(tmp_path):
    import json
    import subprocess
    import sys

    blob = snapshot()
    path = tmp_path / "official-source-copy.csv"
    path.write_bytes(blob)
    process = subprocess.run(
        [sys.executable, "-m", "parlay_bot.research.dkex_reports",
         "--file", str(path), "--date", "2026-10-28",
         "--kind", "settlement", "--sha256", hashlib.sha256(blob).hexdigest(),
         "--symbol-column", "Market", "--settlement-column", "Settlement Price"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(process.stdout)
    assert data["rows"] == 3
    assert data["new_rows"] == 0
    assert data["research_only"] is True
    assert data["independently_verified"] is False
    assert data["trading_enabled"] is False
