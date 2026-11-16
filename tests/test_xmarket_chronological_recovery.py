"""Recover original historical folds without skipping the continuous archive."""
from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import xmarket_history as recovery
from tests import test_xmarket_recovery_budget as budget_fixtures
from tests.test_xmarket_recovery_budget import live_bundle
from tests.xmarket_fixtures import NOW, point

native_provider = budget_fixtures.native_provider


def verified_bundle(*, extra_live=0):
    bundle = live_bundle(64 + extra_live)
    originals = live_bundle(64)["samples"]
    bundle["samples"].extend({**row, "origin": "backfill"} for row in originals)
    bundle["created_at"] = NOW.isoformat()
    return bundle


def test_original_production_split_targets_are_fixed_and_resume_across_days():
    now = datetime(2026, 11, 14, 20, tzinfo=UTC)
    state, windows = recovery.chronological_windows(None, now=now)
    assert state == {"schema": recovery.CHRONOLOGICAL_SCHEMA, "as_of_day": "2026-11-14"}
    assert windows == {
        "train": {"before": datetime(2026, 9, 19, tzinfo=UTC),
                  "floor": datetime(2026, 8, 16, tzinfo=UTC)},
        "validation": {"before": datetime(2026, 10, 17, tzinfo=UTC),
                       "floor": datetime(2026, 9, 19, tzinfo=UTC)},
    }
    saved = {**state, **{name + "_before": (w["before"] - timedelta(hours=8)).isoformat()
                        for name, w in windows.items()}}
    next_state, next_windows = recovery.chronological_windows(
        {"chronological_recovery": saved}, now=now + timedelta(days=1),
    )
    assert next_state == state, "new days must not restart the same contracts"
    assert next_windows["train"]["before"].isoformat() == saved["train_before"]
    assert next_windows["validation"]["before"].isoformat() == saved["validation_before"]
    assert next_windows["train"]["floor"] == windows["train"]["floor"] + timedelta(days=1)
    assert recovery.chronological_windows({"backfill_before": "legacy"}, now=now) == (
        state, windows,
    )


@pytest.mark.parametrize("key,value", [
    ("schema", "wrong"), ("as_of_day", "2026-11-13"), ("as_of_day", "20261112"),
    ("as_of_day", None), ("as_of_day", "yesterday"),
    ("train_before", "2026-09-18T00:01:00+00:00"),
    ("train_before", "2026-09-18T00:00:00"),
    ("train_before", "2026-10-01T00:00:00+00:00"),
    ("validation_before", "2026-09-01T00:00:00+00:00"),
    ("validation_before", "2026-10-15T00:00:00.001+00:00"),
    ("unexpected", "private-value"),
])
def test_invalid_chronological_journal_fails_closed(key, value):
    state, windows = recovery.chronological_windows(None, now=NOW)
    raw = {**state, **{name + "_before": w["before"].isoformat() for name, w in windows.items()}}
    raw[key] = value
    with pytest.raises(ValueError):
        recovery.chronological_windows({"chronological_recovery": raw}, now=NOW)


@pytest.mark.parametrize("raw", [None, [], {}, {"schema": recovery.CHRONOLOGICAL_SCHEMA}])
def test_present_but_incomplete_journal_is_not_treated_as_legacy(raw):
    with pytest.raises(ValueError):
        recovery.chronological_windows({"chronological_recovery": raw}, now=NOW)


def test_verified_sources_recover_both_historical_folds_with_the_existing_ceiling(native_provider):
    bundle = verified_bundle(extra_live=12)
    old_bytes = json.dumps(bundle, sort_keys=True)
    _, windows = recovery.chronological_windows(None, now=NOW)
    previous = copy.deepcopy(windows)
    budget = {}
    history, publications, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[bundle], root=native_provider.root,
        before=native_provider.before, max_windows=96, retry_gaps=0,
        chronological=windows, budget_report=budget,
    )
    assert budget["chronological_window_budgets"] == {"train": 70, "validation": 34}
    assert budget["sequential_window_budget"] == 24
    assert budget["overlap_contracts_selected"] == {"polymarket": 0, "kalshi": 0}
    assert budget["deferred_verified_comparison_sources"] == ["polymarket", "kalshi"]
    assert budget["contract_attempts"] == len(native_provider.collected) == 256
    assert budget["contract_attempts"] == budget["contract_attempt_limit"]
    for name, count in (("train", 70), ("validation", 34)):
        assert windows[name]["before"] == previous[name]["before"] - timedelta(minutes=15 * count)
        assert budget["chronological_recovery"][name]["windows_advanced"] == count
    assert cursor == native_provider.before - timedelta(hours=6), "continuous cursor never jumps"
    assert len(history) == 3 * len(publications) == 768 and not errors
    combined = recovery.combine([bundle], history=history)
    assert all(v["status"] == "verified" and v["mismatch_rate"] == 0
               for v in combined["verification"].values())
    assert json.dumps(bundle, sort_keys=True) == old_bytes
    assert "artifact" not in combined


@pytest.mark.parametrize("max_windows,verified", [(0, True), (1, True), (23, True), (96, False)])
def test_small_or_unverified_passes_keep_comparison_priority(native_provider, max_windows, verified):
    _, windows = recovery.chronological_windows(None, now=NOW)
    before = copy.deepcopy(windows)
    budget = {}
    recovery.backfill(
        None, asset="BTC", bundles=[verified_bundle() if verified else live_bundle(64)],
        root=native_provider.root, before=native_provider.before, max_windows=max_windows,
        retry_gaps=0, chronological=windows, budget_report=budget,
    )
    assert budget["contract_attempts"] <= budget["contract_attempt_limit"]
    if not verified:
        # Comparing original live/archive source overlap still gets the
        # full 64 required contracts, but the 40 spare history slots
        # actually advance the immutable training/validation cursors.
        assert budget["overlap_contracts_selected"] == {"polymarket": 64, "kalshi": 64}
        assert budget["chronological_window_budgets"] == {"train": 27, "validation": 13}
        assert budget["sequential_window_budget"] == 24
        for name in ("train", "validation"):
            assert windows[name]["before"] == (
                before[name]["before"]
                - timedelta(minutes=15 * budget["chronological_window_budgets"][name])
            )
    else:
        assert windows == before
        assert budget["chronological_window_budgets"] == {"train": 0, "validation": 0}


def test_unverified_fidelity_still_recovers_folds_in_bounded_remaining_history_budget(
    native_provider,
):
    # Authentic host receipt: two sources each spent 96 overlap and 32
    # sequential requests, zero chronological, for four assets. The repair
    # MUST preserve all 96 overlap comparisons AND make both fold cursors
    # progress within the same 128/source budget.
    bundle = live_bundle(150)
    _, windows = recovery.chronological_windows(None, now=NOW)
    original = copy.deepcopy(windows)
    budget = {}
    history, publications, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[bundle], root=native_provider.root,
        before=native_provider.before, max_windows=96, retry_gaps=0,
        chronological=windows, budget_report=budget,
    )
    assert budget["overlap_contracts_selected"] == {
        "polymarket": 96, "kalshi": 96,
    }
    assert budget["sequential_window_budget"] == 16
    assert budget["chronological_window_budgets"] == {"train": 11, "validation": 5}
    assert budget["contract_attempts"] == budget["contract_attempt_limit"] == 256
    assert len(native_provider.collected) == len(publications) == 256
    assert cursor == native_provider.before - timedelta(minutes=15 * 16)
    for name, count in (("train", 11), ("validation", 5)):
        assert windows[name]["before"] == original[name]["before"] - timedelta(minutes=15 * count)
        assert budget["chronological_recovery"][name]["windows_advanced"] == count
    # More source history is NOT automatically provider-fidelity approval.
    check = recovery.compare_history(bundle["samples"], history)
    assert all(value["status"] == "unverified" for value in check.values())
    assert not errors and "model_approved" not in budget


def test_completed_fold_reallocates_capacity_and_stops_at_its_real_floor(native_provider):
    _, windows = recovery.chronological_windows(None, now=NOW)
    windows["validation"]["before"] = windows["validation"]["floor"]
    windows["train"]["before"] = windows["train"]["floor"] + timedelta(minutes=30)
    budget = {}
    recovery.backfill(
        None, asset="BTC", bundles=[verified_bundle()], root=native_provider.root,
        before=native_provider.before, max_windows=96, retry_gaps=0,
        chronological=windows, budget_report=budget,
    )
    assert budget["chronological_window_budgets"] == {"train": 104, "validation": 0}
    assert budget["chronological_recovery"]["train"]["windows_advanced"] == 2
    assert windows["train"]["before"] == windows["train"]["floor"]
    assert budget["contract_attempts"] == 2 * (2 + 24)
    assert budget["chronological_recovery"]["validation"]["windows_advanced"] == 0


def test_priority_throttle_preserves_its_cursor_and_pauses_later_source_requests(
    native_provider, monkeypatch,
):
    throttled = []
    def collect(client, *, source, **kwargs):
        if source == "polymarket":
            throttled.append(source)
            response = httpx.Response(429, request=httpx.Request("GET", "https://source.test"))
            raise httpx.HTTPStatusError("private-body", request=response.request, response=response)
        return native_provider.collect(client, source=source, **kwargs)
    monkeypatch.setattr(recovery, "collect_contract", collect)
    _, windows = recovery.chronological_windows(None, now=NOW)
    previous = copy.deepcopy(windows)
    pending = {source: set() for source in recovery.PRICE_SEMANTICS}
    budget = {}
    _, _, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[verified_bundle()], root=native_provider.root,
        before=native_provider.before, max_windows=96, retry_gaps=0,
        chronological=windows, pending=pending, budget_report=budget,
    )
    assert windows == previous and cursor == native_provider.before
    assert throttled == ["polymarket"]
    assert budget["paused_sources"] == ["polymarket"]
    assert budget["contract_attempts_by_stage"]["priority_validation"]["polymarket"] == 0
    assert budget["contract_attempts_by_stage"]["backfill"]["polymarket"] == 0
    assert pending["polymarket"] == {
        previous[name]["before"].isoformat() for name in previous
    } | {native_provider.before.isoformat()}
    assert errors[0]["stage"] == "priority_train" and errors[0]["failure_code"] == "http_429"
    assert "private-body" not in json.dumps(errors)


@pytest.mark.parametrize("complete", [False, True])
def test_only_complete_archived_native_grids_replace_another_request(native_provider, complete):
    close = native_provider.before
    samples = [point(source, close - timedelta(minutes=15 - minute), origin="backfill")
               for source in recovery.PRICE_SEMANTICS
               for minute in ((5, 10, 15) if complete else (5, 15))]
    bundle = {"schema": recovery.BUNDLE_SCHEMA, "asset": "BTC", "samples": samples}
    pending = {source: {close.isoformat()} for source in recovery.PRICE_SEMANTICS}
    budget = {}
    history, publications, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[bundle], root=native_provider.root, before=close,
        max_windows=1, retry_gaps=0, pending=pending, budget_report=budget,
    )
    assert len(native_provider.collected) == (0 if complete else 2)
    assert len(history) == (0 if complete else 6)
    assert len(publications) == (0 if complete else 2)
    assert cursor == close - timedelta(minutes=15) and not errors
    assert pending == {source: set() for source in recovery.PRICE_SEMANTICS}
    assert budget["reused_archived_windows"] == {s: int(complete) for s in recovery.PRICE_SEMANTICS}


def test_conflicting_archived_prices_are_not_reused(native_provider):
    bundle = verified_bundle()
    row = next(r for r in bundle["samples"] if r["origin"] == "backfill")
    bundle["samples"].append({**row, "price": .9})
    with pytest.raises(ValueError, match="conflicting immutable"):
        recovery.backfill(None, asset="BTC", bundles=[bundle], root=native_provider.root,
                          before=native_provider.before, max_windows=1)
    assert not native_provider.collected


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", ["original", "chunk", "checkpoint"])
async def test_failed_publication_cannot_advance_chronological_cursors(
    native_provider, monkeypatch, fail_stage,
):
    class Drive:
        def __init__(self):
            self.reports = [recovery.canonical(verified_bundle())]
            self.fail = fail_stage
        async def read_verified_research_reports(self, **kwargs):
            return [(str(i), doc) for i, doc in enumerate(self.reports)], None
        async def ensure_research_output(self, *, artifact, **kwargs):
            payload = artifact.path.read_bytes()
            doc = json.loads(payload)
            stage = "checkpoint" if doc.get("backfill_checkpoint") else "chunk"
            if self.fail == stage:
                raise RuntimeError("publication not acknowledged")
            self.reports.append(payload)
            return SimpleNamespace(file_id=str(len(self.reports)))
    drive = Drive()
    async def publish(drive, publication):
        if drive.fail == "original":
            raise RuntimeError("original not acknowledged")
    monkeypatch.setattr(recovery, "publish", publish)
    observed = []
    original = recovery.backfill
    def collect(*args, **kwargs):
        observed.append(copy.deepcopy(kwargs["chronological"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "backfill", collect)
    kwargs = {"asset": "BTC", "through": NOW, "cache": native_provider.root / "cache", "max_windows": 24}
    with pytest.raises(RuntimeError, match="not acknowledged"):
        await recovery.weekly_inputs(drive, **kwargs)
    assert not any(json.loads(raw).get("backfill_checkpoint") for raw in drive.reports)
    drive.fail = None
    await recovery.weekly_inputs(drive, **kwargs)
    assert observed[0] == observed[1], "staged native chunks cannot acknowledge progress"
    checkpoint = next(json.loads(raw) for raw in drive.reports
                      if json.loads(raw).get("backfill_checkpoint"))
    await recovery.weekly_inputs(drive, **kwargs)
    for name in ("train", "validation"):
        assert observed[2][name]["before"].isoformat() == checkpoint["chronological_recovery"][name + "_before"]
        assert observed[2][name]["before"] < observed[1][name]["before"]
    assert all("chronological_recovery" not in json.loads(raw) for raw in drive.reports
               if not json.loads(raw).get("backfill_checkpoint"))


@pytest.mark.asyncio
async def test_original_candle_readback_and_native_recovery_admit_real_chronological_folds(
    native_provider, monkeypatch, tmp_path,
):
    from parlay_bot.research import archive_training, recorded_candles, xmarket_training
    from parlay_bot.research.audit import audit_partition
    from tests.test_recorded_candle_restoration import Drive, candle

    _, windows = recovery.chronological_windows(None, now=NOW)
    train_end, test_start = windows["train"]["before"], windows["validation"]["before"]
    bundle = verified_bundle()
    history, _, _, errors = recovery.backfill(
        None, asset="BTC", bundles=[bundle], root=native_provider.root,
        before=native_provider.before, max_windows=96, retry_gaps=0, chronological=windows,
    )
    assert not errors
    native = recovery.combine([bundle], history=history)
    native["live_venues"] = ["bitget", "okx", "gate"]
    recent_end = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC)
    ranges = [(train_end - timedelta(hours=60), train_end + timedelta(hours=2)),
              (test_start - timedelta(hours=50), test_start + timedelta(hours=2)),
              (recent_end - timedelta(hours=60), recent_end)]
    rows = []
    for venue in native["live_venues"]:
        for fold, (start, end) in enumerate(ranges):
            if venue == "gate" and fold == 0:
                continue  # Matches production: Gate's original September archive is absent.
            for i in range(int((end - start).total_seconds() // 300)):
                rows.append(candle(start + timedelta(minutes=5 * i), venue=venue,
                                   close=100 + .2 * math.sin(i / 5) + i * .0001))
    root = tmp_path / "restored"
    restored = await recorded_candles.restore_recorded_partitions(
        Drive(rows), asset="BTC", root=root, first_day=ranges[0][0].date(),
        last_day=NOW.date(), now=NOW + timedelta(days=1),
    )
    assert restored["source_files"]
    for manifest in restored["restored"]:
        assert audit_partition(Path(manifest))["full_calendar_month_verified"] is False
    frames, provenance = archive_training.load_frames_manifests(root, target_asset="BTC")
    def forbidden(*args, **kwargs):
        raise AssertionError("original-source admission cannot fit or publish a model")
    monkeypatch.setattr(xmarket_training, "_fit_logistic", forbidden)
    monkeypatch.setattr(xmarket_training, "_standardize_fit", forbidden)
    result = xmarket_training.train_run(
        frames, asset="BTC", horizon=60, train_end=train_end, test_start=test_start,
        trained_through=recent_end, provenance=provenance, code_version="fixture",
        market_data=native, admission_only=True,
    )
    report = result["report"]
    assert report["dispersion_venues"] == ["bitget", "okx"]
    assert report["input_count"] == 18 and report["training_status"] == "admission_only"
    assert report["overlap_evidence"]["eligible"] is True
    assert all(v["shortfall"] == 0 for v in report["overlap_evidence"]["splits"].values())
    assert report["gates_passed"] is False and result["artifact"] is None
    assert report["provenance"]["recorded_partitions"]["bitget:2026-09"]["source_files"]
