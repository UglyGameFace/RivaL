"""Proof that historical open-interest labels never impersonate live features."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import prepare
from parlay_bot.research.supplemental_metrics import (
    SOURCE,
    _days,
    collect_daily_metrics,
    daily_metrics_url,
    ingest_daily_metrics,
    parse_daily_metrics,
)

DAY_OLD = date(2026, 6, 24)
DAY_NEW = date(2026, 6, 25)


def metrics_csv(day, *, post=True, wrong_symbol=False):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
        "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    ])
    when = datetime.combine(day, datetime.min.time()).isoformat(sep=" ")
    when2 = (datetime.combine(day, datetime.min.time()) + timedelta(minutes=5)).isoformat(sep=" ")
    for index, stamp in enumerate((when, when2)):
        writer.writerow([
            stamp, "ETHUSDT" if wrong_symbol else "BTCUSDT",
            120 + index, 5000, ".8", "1.1", "1.2", ".9",
        ])
    return output.getvalue().encode()


def mock_daily(url, raw):
    filename = url.rsplit("/", 1)[-1]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as obj:
        obj.writestr(filename[:-4] + ".csv", raw)
    archive = buf.getvalue()
    sha = hashlib.sha256(archive).hexdigest()
    def handler(request):
        if str(request.url) == url + ".CHECKSUM":
            return httpx.Response(200, text=sha + "  " + filename)
        if str(request.url) == url:
            return httpx.Response(200, content=archive)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_regime_flip_preserves_distinct_safe_feature_clocks():
    old = parse_daily_metrics(metrics_csv(DAY_OLD), asset="BTC", day=DAY_OLD)
    new = parse_daily_metrics(metrics_csv(DAY_NEW), asset="BTC", day=DAY_NEW)
    assert old[0]["label_regime"] == "end_labeled"
    assert new[0]["label_regime"] == "start_labeled"
    old_gap = (datetime.fromisoformat(old[0]["conservative_feature_after_at"])
               - datetime.fromisoformat(old[0]["source_labeled_at"]))
    new_gap = (datetime.fromisoformat(new[0]["conservative_feature_after_at"])
               - datetime.fromisoformat(new[0]["source_labeled_at"]))
    assert old_gap == timedelta(minutes=5)
    assert new_gap == timedelta(minutes=10)
    assert old[0]["boundary_caution"] and new[0]["boundary_caution"]


@pytest.mark.parametrize("day", [DAY_OLD, DAY_NEW, date(2026, 9, 17)])
def test_checksum_verified_metrics_remain_offline_and_publishable(tmp_path, day):
    url = daily_metrics_url(asset="BTC", day=day)
    with mock_daily(url, metrics_csv(day)) as client:
        one = ingest_daily_metrics(client, asset="BTC", day=day, out_dir=tmp_path)
        again = ingest_daily_metrics(client, asset="BTC", day=day, out_dir=tmp_path)
    assert one == again and one.records == 2
    manifest = json.loads(one.manifest_path.read_text())
    assert manifest["checksum_verified"]
    assert manifest["prospective_forecast"] is False
    assert manifest["timestamp_caution"]
    assert prepare(one.manifest_path).source == SOURCE


def test_checksum_verified_metrics_unsorted_source_keeps_original_row_order():
    original = metrics_csv(DAY_NEW)
    lines = original.splitlines(keepends=True)
    assert len(lines) == 3
    shuffled = b"".join([lines[0], lines[2], lines[1]])
    ordered = parse_daily_metrics(shuffled, asset="BTC", day=DAY_NEW)
    assert len(ordered) == 2
    assert [r["source_row_index"] for r in ordered] == [2, 1]
    assert ordered[0]["source_labeled_at"] < ordered[1]["source_labeled_at"]


def test_duplicate_historical_positioning_labels_rejected_even_if_unsorted():
    original = metrics_csv(DAY_NEW)
    lines = original.splitlines(keepends=True)
    duplicated = b"".join([lines[0], lines[2], lines[1], lines[2]])
    with pytest.raises(HistoryImportError, match="duplicate"):
        parse_daily_metrics(duplicated, asset="BTC", day=DAY_NEW)


def test_bad_symbol_or_negative_metrics_are_rejected():
    with pytest.raises(HistoryImportError):
        parse_daily_metrics(metrics_csv(DAY_NEW, wrong_symbol=True),
                            asset="BTC", day=DAY_NEW)
    with pytest.raises(HistoryImportError):
        parse_daily_metrics(metrics_csv(DAY_NEW).replace(b",5000,", b",-1,"),
                            asset="BTC", day=DAY_NEW)


def test_batch_uses_bounded_retries_and_reports_gaps(tmp_path):
    dates = [date(2026, 6, 24), date(2026, 6, 25)]
    url = daily_metrics_url(asset="BTC", day=dates[0])
    # Reuse a controlled bad source for all requests.
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404),
    )) as client:
        status = collect_daily_metrics(client, root=tmp_path, days=dates,
                                       assets=["BTC", "ETH"], max_new=2)
    assert status["planned"] == 4 and status["rejected"] == 2
    assert status["remaining"] == 2 and status["downloaded"] == 0
    assert not list(tmp_path.rglob("*.ndjson"))
    assert url.endswith("BTCUSDT-metrics-2026-06-24.zip")


def test_days_exclude_today_and_reject_future_dates():
    today = datetime(2026, 10, 31, 16, tzinfo=UTC)
    days = _days(3, now=today)
    assert days == [date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)]
    assert date(2026, 10, 31) not in days
    with pytest.raises(HistoryImportError):
        daily_metrics_url(asset="BTC", day=date(2099, 1, 1))
