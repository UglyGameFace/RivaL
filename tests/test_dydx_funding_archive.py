"""Actual dYdX v4 source funding must retain UTC, ticker and causal identity."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.dydx_funding_archive import (
    LIMIT,
    automatic_month,
    complete_hourly_month,
    month_bounds,
    validate_page,
)
from parlay_bot.research.free_history import HistoryImportError


def page(rows):
    return json.dumps({"historicalFunding": rows}).encode()


def funding(time, *, ticker="BTC-USD", rate="0.0001", price="38000"):
    return {"ticker": ticker, "effectiveAt": time, "rate": rate,
            "price": price, "effectiveAtHeight": "100"}


def test_real_hourly_rate_and_oracle_price_shape():
    start, end = month_bounds("2023-11")
    rows = [funding("2023-11-03T01:00:00.000Z"),
            funding("2023-11-03T00:00:00.000Z")]
    stamps = validate_page(page(rows), asset="BTC", cursor=end, start=start, end=end)
    assert len(stamps) == 2
    assert stamps[0] - stamps[1] == timedelta(hours=1)
    assert LIMIT == 100


@pytest.mark.parametrize("rows", [
    [funding("2023-11-03T01:00:00.000Z", ticker="SOL-USD")],
    [funding("2023-11-03T01:00:00.000Z", price="0")],
    [funding("2023-11-03T01:00:00.000Z", rate="NaN")],
    [funding("2023-11-03T01:00:00.000Z", rate="10")],
    [funding("2023-11-03T01:00:00.000Z"),
     funding("2023-11-03T01:00:00.000Z")],
    [funding("2023-12-02T01:00:00.000Z")],
])
def test_reject_forged_wrong_price_time_or_duplicate(rows):
    start, end = month_bounds("2023-11")
    with pytest.raises(HistoryImportError):
        validate_page(page(rows), asset="BTC", cursor=end, start=start, end=end)


def test_chain_launch_and_future_restrictions():
    for month in ("2021-01", "2023-10", "2026-11", "2023-13", "202311"):
        with pytest.raises(HistoryImportError):
            month_bounds(month)


def test_invalid_schema_and_page_budget():
    start, end = month_bounds("2023-11")
    for raw in (b"{}", b"not-json", page({}), page([1]), b" " * 2_000_001):
        with pytest.raises(HistoryImportError):
            validate_page(raw, asset="BTC", cursor=end, start=start, end=end)
    with pytest.raises(HistoryImportError):
        validate_page(page([funding("2023-11-03T01:00:00.000Z")]*101),
                      asset="BTC", cursor=end, start=start, end=end)

def test_actual_dydx_chain_settlement_seconds_after_each_hour_are_complete():
    start, end = month_bounds("2023-11")
    hours = int((end - start).total_seconds() // 3600)
    stamps = [start + timedelta(hours=i, seconds=1, milliseconds=59)
              for i in range(hours)]
    assert complete_hourly_month(stamps, start=start, end=end,
                                 crossed_boundary=True)
    assert not complete_hourly_month(stamps, start=start, end=end,
                                     crossed_boundary=False)
    assert not complete_hourly_month(stamps[:-1], start=start, end=end,
                                     crossed_boundary=True)
    doubled = stamps[:-1] + [stamps[-2] + timedelta(milliseconds=1)]
    assert not complete_hourly_month(doubled, start=start, end=end,
                                     crossed_boundary=True)


def test_month_rotation_covers_all_completed_v4_months_without_future_dates():
    start = datetime(2026, 11, 1, tzinfo=UTC)
    selected = {
        automatic_month(start + timedelta(hours=4 * i))
        for i in range(36)
    }
    assert len(selected) == 36
    assert "2023-11" in selected
    assert "2026-10" in selected
    assert "2026-11" not in selected
