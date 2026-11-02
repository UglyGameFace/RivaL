"""Lighter native funding seconds and mark-price milliseconds are distinct."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.lighter_auxiliary_archive import (
    active_ids,
    automatic_month,
    latest_full_month,
    month_bounds,
    validate,
)


def test_lighter_2025_launch_and_cycle():
    for month in ("2021-01", "2025-01", "2026-11", "2025-13"):
        with pytest.raises(HistoryImportError):
            month_bounds(month)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    found = {automatic_month(start + timedelta(hours=6 * i)) for i in range(21)}
    assert len(found) == 21
    assert "2025-02" in found and "2026-10" in found


def test_latest_completed_month_and_recent_first_rotation():
    assert latest_full_month(datetime(2026, 11, 2, tzinfo=UTC)) == "2026-10"
    start = datetime(2026, 11, 1, tzinfo=UTC)
    periods = [automatic_month(start + timedelta(hours=6 * i))
               for i in range(21)]
    assert len(set(periods)) == 21
    index = periods.index("2026-10")
    rotated = periods[index:] + periods[:index]
    assert rotated[:6] == [
        "2026-10", "2026-09", "2026-08",
        "2026-07", "2026-06", "2026-05",
    ]


def test_funding_validates_off_window_older_rows_without_admitting_them():
    start, end = month_bounds("2025-02")
    older = int((start - timedelta(hours=3)).timestamp())
    inside = int((start + timedelta(hours=2)).timestamp())
    raw = json.dumps({"code": 200, "fundings": [
        {"timestamp": older, "rate": "0.001", "direction": "long", "value": "1"},
        {"timestamp": inside, "rate": "0.001", "direction": "short", "value": "1"},
    ]}).encode()
    times, count = validate(raw, family="funding", begin=start, end=end)
    assert times == {inside} and count == 1
    for invalid in (
        [{"timestamp": int(end.timestamp()), "rate": "0.001",
          "direction": "long", "value": "1"}],
        [{"timestamp": inside, "rate": "0.001", "direction": "long", "value": "1"}] * 2,
    ):
        with pytest.raises(HistoryImportError, match="after-window/duplicated"):
            validate(json.dumps({"code": 200, "fundings": invalid}).encode(),
                     family="funding", begin=start, end=end)


def test_mark_candle_older_original_rows_not_mislabeled_as_in_window():
    start, end = month_bounds("2026-09")
    inside = int((start + timedelta(hours=2)).timestamp()) * 1000
    older = int((start - timedelta(hours=2)).timestamp()) * 1000
    row = lambda stamp: {"t": stamp, "o": "100", "h": "102",
                         "l": "98", "c": "101"}
    raw = json.dumps({"code": 200, "c": [row(older), row(inside)]}).encode()
    times, count = validate(raw, family="mark_price", begin=start, end=end)
    assert times == {inside // 1000} and count == 1


def test_only_active_perp_market_ids_accepted():
    payload = {"code": 200, "order_books": [
        {"market_type": "perp", "status": "active", "symbol": "BTC", "market_id": 1},
        {"market_type": "perp", "status": "active", "symbol": "ETH", "market_id": 0},
        {"market_type": "spot", "status": "active", "symbol": "SOL", "market_id": 2},
    ]}
    assert active_ids(json.dumps(payload).encode()) == {"BTC": 1, "ETH": 0}
    payload["order_books"].append(payload["order_books"][0])
    with pytest.raises(HistoryImportError):
        active_ids(json.dumps(payload).encode())


def test_original_funding_second_timestamp_with_direction():
    start, end = month_bounds("2025-02")
    t = int(start.timestamp())
    body = json.dumps({"code": 200, "fundings": [{
        "timestamp": t, "rate": "0.0012", "value": "0.9",
        "direction": "long",
    }]}).encode()
    times, count = validate(body, family="funding", begin=start, end=end)
    assert times == {t} and count == 1
    with pytest.raises(HistoryImportError):
        validate(body.replace(b'"long"', b'"bad"'),
                 family="funding", begin=start, end=end)


def test_mark_candle_millisecond_source_and_ohlc():
    start, end = month_bounds("2025-02")
    t = int(start.timestamp())
    body = json.dumps({"code": 200, "c": [{
        "t": t * 1000, "o": "100", "h": "105", "l": "95", "c": "103",
    }]}).encode()
    times, count = validate(body, family="mark_price", begin=start, end=end)
    assert count == 1 and times == {t}
    with pytest.raises(HistoryImportError):
        validate(body.replace(b'"105"', b'"90"'), family="mark_price",
                 begin=start, end=end)
