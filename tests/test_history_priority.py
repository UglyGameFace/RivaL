"""Cross-collector rolling-two-year priority without deleting older history."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from parlay_bot.research.history_priority import ordered_months, window


def test_november_2_2026_two_year_archive_window_is_24_completed_months():
    clock = datetime(2026, 11, 2, 13, 26, tzinfo=UTC)
    w = window(clock)
    assert w.exact_rolling_start_utc == "2024-11-02T13:26:00+00:00"
    assert w.first_completed_month == "2024-11"
    assert w.last_completed_month == "2026-10"
    assert len(w.completed_months) == 24
    assert w.live_partial_month == "2026-11"
    assert "2026-11" not in w.completed_months
    assert "2021-01" not in w.completed_months
    assert w.metadata()["contract_listing_dates_proven_by_policy"] is False
    assert w.metadata()["live_model_admission_allowed"] is False


def test_recent_six_then_rest_of_two_year_window_then_preserve_older():
    clock = datetime(2026, 11, 2, 13, tzinfo=UTC)
    months = [
        "2021-01", "2023-01", "2024-10", "2024-11",
        "2025-01", "2026-01", "2026-04", "2026-05", "2026-06",
        "2026-07", "2026-08", "2026-09", "2026-10",
    ]
    ordered = ordered_months(months, now=clock)
    assert ordered[:6] == [
        "2026-10", "2026-09", "2026-08",
        "2026-07", "2026-06", "2026-05",
    ]
    assert ordered[6:10] == [
        "2026-04", "2026-01", "2025-01", "2024-11",
    ]
    assert ordered[-3:] == ["2024-10", "2023-01", "2021-01"]
    assert set(ordered) == set(months)


def test_leap_day_and_utc_timezone_are_explicit():
    leap = window(datetime(2028, 2, 29, 12, tzinfo=UTC))
    assert leap.exact_rolling_start_utc == "2026-02-28T12:00:00+00:00"
    assert leap.first_completed_month == "2026-02"
    assert leap.last_completed_month == "2028-01"
    with pytest.raises(ValueError, match="aware"):
        window(datetime(2026, 11, 2))
    with pytest.raises(ValueError, match="duplicate"):
        ordered_months(["2026-10", "2026-10"])
    with pytest.raises(ValueError, match="completed"):
        ordered_months(["2026-11"], now=datetime(2026, 11, 2, tzinfo=UTC))


def test_prelisting_period_not_fabricated_or_assumed_present():
    ordered = ordered_months(
        ["2025-01", "2025-02", "2026-10"],
        now=datetime(2026, 11, 2, tzinfo=UTC),
    )
    assert ordered == ["2026-10", "2025-02", "2025-01"]
    assert "2024-11" not in ordered  # caller's actual source launch floor
