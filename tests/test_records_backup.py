from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest

from parlay_bot.perpetuals import host_memory
from parlay_bot.perpetuals.records_backup import RecordsBackup, file_name
from parlay_bot.storage.hot import SQLiteHotStore


@dataclass
class Stored:
    file_id: str


class FakeDrive:
    def __init__(self):
        self.saved = {}

    async def ensure_history_file(self, *, artifact, batch_id, mime_type):
        with gzip.open(artifact.path, "rt") as handle:
            self.saved[batch_id] = [json.loads(line) for line in handle]
        return Stored(file_id=f"id-{len(self.saved)}")


@pytest.fixture(autouse=True)
def _no_memory_pressure(monkeypatch):
    # run_once pauses under host memory pressure. Pin a calm host so these
    # tests do not depend on the test runner's RSS/cgroup usage (in CI the
    # earlier LightGBM-trained fixtures can push the runner past 83% of the
    # 512 MB fallback limit).
    calm = host_memory.HostMemory(rss_bytes=0, limit_bytes=512 * host_memory.MIB,
                                  oom_kills=0)
    monkeypatch.setattr(host_memory, "snapshot", lambda: calm)


@pytest.mark.asyncio
async def test_memory_pressure_pauses_the_backup(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "busy.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE rival_intelligence_calls (asset TEXT, issued_at TEXT)")
            conn.execute("INSERT INTO rival_intelligence_calls VALUES "
                         "('BTC', '2026-11-02T10:00:00+00:00')")
        tight = host_memory.HostMemory(rss_bytes=500 * host_memory.MIB,
                                       limit_bytes=512 * host_memory.MIB, oom_kills=0)
        monkeypatch.setattr(host_memory, "snapshot", lambda: tight)
        drive = FakeDrive()
        backup = RecordsBackup(store, drive, tmp_path / "work")
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        assert await backup.run_once(now) == {"saved": 0, "rows": 0, "failed": 0}
        assert drive.saved == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_each_completed_day_is_saved_once(tmp_path):
    store = SQLiteHotStore(tmp_path / "records.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE rival_intelligence_calls (asset TEXT, issued_at TEXT)")
            for when in ("2026-11-01T10:00:00+00:00", "2026-11-01T23:59:00+00:00",
                         "2026-11-02T00:01:00+00:00", "2026-11-03T09:00:00+00:00"):
                conn.execute("INSERT INTO rival_intelligence_calls VALUES ('BTC', ?)", (when,))
        drive = FakeDrive()
        backup = RecordsBackup(store, drive, tmp_path / "work")
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        result = await backup.run_once(now)
        # Nov 1 and Nov 2 are complete; Nov 3 is still in progress.
        assert result == {"saved": 2, "rows": 3, "failed": 0}
        assert len(drive.saved["records-rival_intelligence_calls-2026-11-01"]) == 2
        assert len(drive.saved["records-rival_intelligence_calls-2026-11-02"]) == 1
        assert set(drive.saved) == {"records-rival_intelligence_calls-2026-11-01",
                                    "records-rival_intelligence_calls-2026-11-02"}
        await backup.run_once(now)  # older, empty days: markers only, no files
        assert len(drive.saved) == 2
        assert not list((tmp_path / "work").glob("*"))  # temp files removed
        assert file_name("perp_liquidity_5m", date(2026, 11, 1)) == \
            "rival-records__perp_liquidity_5m__2026-11-01.ndjson.gz"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_failed_read_is_retried_not_marked_empty(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "flaky.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE rival_intelligence_calls (asset TEXT, issued_at TEXT)")
            conn.execute("INSERT INTO rival_intelligence_calls VALUES "
                         "('BTC', '2026-11-02T10:00:00+00:00')")
        backup = RecordsBackup(store, FakeDrive(), tmp_path / "work")
        real = backup._write

        def flaky(table, *args):
            if table == "rival_intelligence_calls":
                raise TimeoutError("database busy")
            return real(table, *args)

        monkeypatch.setattr(backup, "_write", flaky)
        now = datetime(2026, 11, 3, 12, tzinfo=UTC)
        assert (await backup.run_once(now))["failed"] >= 1
        with store.connect() as conn:
            marked = conn.execute("SELECT COUNT(*) FROM rival_records_backup "
                                  "WHERE table_name='rival_intelligence_calls'").fetchone()[0]
        assert marked == 0  # retried next run, not recorded as an empty day
        monkeypatch.setattr(backup, "_write", real)
        drive = backup.drive
        await backup.run_once(now)
        assert "records-rival_intelligence_calls-2026-11-02" in drive.saved
    finally:
        store.close()
