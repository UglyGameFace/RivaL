"""Source restore must use the date frozen when the collector started."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tools import restore_archive_months


def test_anchored_restore_keeps_original_calendar_on_delayed_runner(
        monkeypatch, tmp_path):
    observed = {}

    def complete(*, now, count):
        observed["month_clock"] = now
        assert count == 6
        return ["2026-10", "2026-09"]

    async def restore(**kwargs):
        observed["restore"] = kwargs
        return {"restored": {}, "excluded": {},
                "okx_selected_source_integrity": kwargs["okx_source_integrity"]}

    monkeypatch.setattr(restore_archive_months, "complete_months", complete)
    monkeypatch.setattr(restore_archive_months, "restore_verified_months", restore)
    assert restore_archive_months.main([
        "--asset", "ETH", "--as-of", "2026-11-16",
        "--okx-source-integrity", "okx-v5-public-history-5m-v1",
        "--root", str(tmp_path),
    ]) == 0
    report = observed["restore"]
    assert observed["month_clock"] == datetime(2026, 11, 16, tzinfo=UTC)
    assert report["now"] == observed["month_clock"]
    assert report["records_through"] == date(2026, 11, 15)
    assert report["records_since"] == date(2026, 11, 16) - timedelta(days=90)
    assert report["months"] == ["2026-10", "2026-09"]


def test_future_source_clock_fails_before_drive_access(monkeypatch, tmp_path):
    async def forbidden(**kwargs):
        pytest.fail("future source date must never touch private Drive")

    monkeypatch.setattr(restore_archive_months, "restore_verified_months", forbidden)
    with pytest.raises(SystemExit):
        restore_archive_months.main([
            "--asset", "BTC", "--as-of", "2099-01-01",
            "--root", str(tmp_path),
        ])
