from __future__ import annotations

from datetime import UTC, datetime

import httpx

from parlay_bot.research.batch import _already_present, collect, complete_months, targets
from parlay_bot.research.free_history import _persist


def test_six_complete_months_cross_year_boundary():
    months = complete_months(now=datetime(2026, 2, 14, tzinfo=UTC), count=6)
    assert months == [
        "2025-08", "2025-09", "2025-10",
        "2025-11", "2025-12", "2026-01",
    ]
    assert complete_months(now=datetime(2026, 10, 28, tzinfo=UTC), count=1) == [
        "2026-09",
    ]


def test_batch_plan_crosses_multiple_domains_with_no_source_conflation():
    jobs = targets(
        months=["2026-08", "2026-09"], assets=["BTC", "ETH"],
        intervals=["5m", "1h"], seasons=["2526"], leagues=["E0", "D1"],
    )
    assert len(jobs) == 10
    assert jobs[0] == ("binance", "BTC", "5m", "2026-08")
    assert jobs[-1] == ("football", "D1", "2526", "")


def test_resumption_checks_file_digest_and_never_trusts_manifest_alone(tmp_path):
    job = ("football", "E0", "2526", "")
    record = {"example": "safe historical test fixture"}
    created = _persist(
        source="football_data", key="E0-2526", records=[record],
        raw_sha="x" * 64, url="https://www.football-data.co.uk/mmz4281/2526/E0.csv",
        out_dir=tmp_path,
    )
    assert _already_present(tmp_path, job)
    assert created.manifest_path.is_file()
    created.data_path.write_bytes(created.data_path.read_bytes() + b"bad")
    assert not _already_present(tmp_path, job)


def test_collection_budget_counts_failed_downloads_and_reports_them(tmp_path):
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(404)
    jobs = [
        ("football", "E0", "2526", ""),
        ("football", "D1", "2526", ""),
        ("football", "I1", "2526", ""),
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = collect(client, out=tmp_path, jobs=jobs, max_new=2)
    assert results["planned"] == 3
    assert results["rejected"] == 2
    assert results["remaining"] == 1
    assert results["already_verified"] == 0
    assert len(calls) == 2
    assert len(results["failed"]) == 2
    assert not list(tmp_path.rglob("*.ndjson"))


def test_verified_research_plan_can_include_xrp_live_contract():
    jobs = targets(
        months=["2026-08", "2026-09"],
        assets=["BTC", "ETH", "SOL", "XRP"],
        intervals=["5m"], seasons=[], leagues=[],
    )
    assert len(jobs) == 8
    assert all(job[0] == "binance" for job in jobs)
    assert ("binance", "XRP", "5m", "2026-09") in jobs


def test_research_publication_grace_excludes_just_closed_unpublished_month():
    from datetime import UTC, datetime
    from parlay_bot.research.production import publication_ready_months

    assert publication_ready_months(
        now=datetime(2026, 11, 1, tzinfo=UTC), count=6, grace_days=7,
    ) == ["2026-04", "2026-05", "2026-06",
          "2026-07", "2026-08", "2026-09"]
    assert publication_ready_months(
        now=datetime(2026, 11, 8, tzinfo=UTC), count=6, grace_days=7,
    ) == ["2026-05", "2026-06", "2026-07",
          "2026-08", "2026-09", "2026-10"]
    import pytest
    with pytest.raises(ValueError, match="grace"):
        publication_ready_months(
            now=datetime(2026, 11, 8, tzinfo=UTC), count=6, grace_days=15,
        )
