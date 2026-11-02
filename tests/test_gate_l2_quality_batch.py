"""Strict source-only quality and resumable Gate hourly L2 coverage contracts."""
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parlay_bot.research import gate_hourly_l2_batch as batch
from parlay_bot.research import gate_l2_quality as quality
from parlay_bot.research.free_history import HistoryImportError

HOUR = "2023042505"
CLOCK = datetime(2026, 11, 2, 12, tzinfo=UTC)


def _snapshot(*, current=1682398802.134, seq=36309008890, crossed=False):
    return {
        "current": current, "update": current - .003, "id": seq,
        "asks": [{"p": "27403" if crossed else "27404", "s": 2},
                 {"p": "27404.2", "s": 1}],
        "bids": [{"p": "27403.9", "s": 3},
                 {"p": "27403.8", "s": 6}],
    }


def _gzip(*rows):
    return gzip.compress(("".join(json.dumps(row) + "\n" for row in rows)).encode())


def test_actual_snapshot_schema_replay_checks_crossed_and_time_order():
    first = _snapshot()
    second = _snapshot(current=1682398803.2, seq=36309008925)
    report = quality.inspect_snapshot_hour(_gzip(first, second), hour=HOUR)
    assert report["observed_snapshots"] == 2
    assert report["noncrossed_books_verified"]
    assert report["strict_source_time_and_id_increase"]
    assert report["minimum_bid_depth_levels"] == 2
    assert report["minimum_ask_depth_levels"] == 2
    assert report["max_observed_snapshot_gap_seconds"] == pytest.approx(1.066, abs=.002)
    assert report["full_hourly_source_continuity_verified"] is False
    assert report["model_training_eligible"] is False

    for bad in (
        _snapshot(crossed=True),
        {**first, "id": True},
        {**first, "bids": [{"p": "1", "s": -1}]},
        {**first, "bids": [{"p": "2", "s": 1}, {"p": "3", "s": 1}]},
        {**first, "current": 1682398800 - 1},
    ):
        with pytest.raises(HistoryImportError):
            quality.inspect_snapshot_hour(_gzip(bad), hour=HOUR)

    with pytest.raises(HistoryImportError, match="duplicate/retrograde"):
        quality.inspect_snapshot_hour(_gzip(first, first), hour=HOUR)


def test_gap_observations_not_synthetic_interval_fill():
    rows = (_snapshot(), _snapshot(current=1682398850.134, seq=36309009300))
    report = quality.inspect_snapshot_hour(_gzip(*rows), hour=HOUR)
    assert report["observed_snapshots"] == 2
    assert report["gaps_over_five_seconds"] == 1
    assert report["max_observed_snapshot_gap_seconds"] == pytest.approx(48)
    assert report["snapshot_interpolation_performed"] is False


def test_source_month_stratification_covers_2021_to_last_completed_month():
    hours = batch.historical_month_anchors(CLOCK)
    assert hours[0] == "2026102505"
    assert hours[-1] == "2021082505"
    assert len(hours) == len(set(hours))
    assert all(batch.hour_bounds(hour, now=CLOCK) for hour in hours)
    slots = batch.candidate_slots(CLOCK)
    assert len(slots) == len(hours) * 8
    assert {family for _, family, _ in slots} == {"orderbooks_slice", "orderbooks"}
    assert {asset for asset, _, _ in slots} == set(batch.ASSETS)


def test_remote_manifest_only_skips_exact_source_family_hour():
    raw = ("gate-hourly-l2-BTC-orderbooks_slice-2023042505--"
           "a927b654025a97ea.gz.manifest.json")
    other = raw.replace("orderbooks_slice", "orderbooks")
    listed = batch.existing_hours([raw, raw, "unrelated", other])
    assert listed == {
        ("BTC", "orderbooks_slice", HOUR), ("BTC", "orderbooks", HOUR),
    }
    selected, skipped = batch.select_slots(CLOCK, set(), max_attempts=12)
    assert len(selected) == len(set(selected)) == 12
    assert skipped == 0
    selected_after, skipped_after = batch.select_slots(
        CLOCK, {selected[0]}, max_attempts=12,
    )
    assert len(selected_after) == 12
    assert selected[0] not in selected_after
    assert skipped_after >= 1
    with pytest.raises(HistoryImportError):
        batch.select_slots(CLOCK, set(), max_attempts=13)


@pytest.mark.asyncio
async def test_actual_batch_retains_true_readback_and_unavailable_sources(
    monkeypatch, tmp_path: Path,
):
    async def names(_drive):
        return ["gate-hourly-l2-BTC-orderbooks-2023042505--"
                + "b" * 16 + ".gz.manifest.json"]

    class Drive:
        async def aclose(self):
            pass

    def download(_client, *, asset, family, hour, now):
        if asset == "SOL":
            raise HistoryImportError("original HTTP 404")
        return b"original", {"original_source_sha256": "stub"}

    async def store(_drive, *, raw, asset, family, hour, evidence, root, now):
        return {"asset": asset, "source_sha": "verified", "drive_sha256_readback_verified": True}

    monkeypatch.setattr(batch, "_drive", lambda _: Drive())
    monkeypatch.setattr(batch, "list_names", names)
    monkeypatch.setattr(batch, "download", download)
    monkeypatch.setattr(batch, "store", store)

    report = await batch.collect_batch(
        now=CLOCK, root=tmp_path, report_path=tmp_path / "batch.json",
        selected_hour=HOUR,
    )
    assert report["already_listed_not_byte_reverified"] == 1
    assert len(report["readbacks"]) == 5
    assert len(report["blocked"]) == 2
    assert report["original_source_bytes_uploaded"] == 5 * len(b"original")
    assert report["source_months_qualified"] == 0
    assert report["future_only_model_approval"] is False
    assert (tmp_path / "batch.json").exists()
