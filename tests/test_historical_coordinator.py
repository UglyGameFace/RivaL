"""Cross-venue historical queue cannot mix listing dates, evidence or months."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from parlay_bot.research.drive_source_census import summarize
from parlay_bot.research.historical_coordinator import (
    ATTEMPT_FLOORS,
    build_plan,
    completed_range,
)
from parlay_bot.research.source_native import publication_ranges
from parlay_bot.research.source_native_offline import select_training_months
from tools.historical_dispatch import run_plan, should_dispatch, should_recover_failed

NOW = datetime(2026, 11, 2, 14, tzinfo=UTC)


def _scope(plan, venue, asset, family):
    return next(row for row in plan["scopes"]
                if (row["venue"], row["asset"], row["family"]) ==
                (venue, asset, family))


def _name(asset: str, family: str, month: str, sha: str = "a") -> str:
    return f"bitget-v3-{asset}-{family}-{month}--{sha * 16}.manifest.json"


def test_all_ten_venues_and_640_independent_scopes_without_false_completeness():
    plan = build_plan(summarize([], now=NOW))
    assert plan["scope_count"] == 640
    assert len(plan["venues"]) == 10
    assert len(plan["assets"]) == 4
    assert not plan["all_history_complete"]
    assert not plan["contract_listing_dates_verified_in_this_report"]
    for row in plan["scopes"]:
        assert row["actual_contract_listing_month"] is None
        assert row["verified_complete_months_from_this_census"] == 0
        assert row["no_name_means_unavailable"] is False
    assert _scope(plan, "bitget", "BTC", "candles_5m")[
        "earliest_technical_attempt_month"] == "2024-11"
    assert _scope(plan, "okx", "BTC", "trades")[
        "earliest_technical_attempt_month"] == "2021-09"
    assert _scope(plan, "okx", "BTC", "candles_5m")[
        "earliest_technical_attempt_month"] == "2023-07"
    assert _scope(plan, "dydx", "BTC", "funding")[
        "earliest_technical_attempt_month"] == "2023-11"
    assert _scope(plan, "hyperliquid", "BTC", "liquidations")[
        "collector_mode"] == "collector_not_yet_integrated"


def test_contract_listing_unknown_even_with_source_names_and_independent_coin():
    names = [_name("BTC", "market", "2025-06")]
    names += ["BTCUSDT-5m-2021-01--" + "f" * 16 + ".manifest.json"]
    plan = build_plan(summarize(names, now=NOW))
    assert _scope(plan, "bitget", "BTC", "candles_5m")[
        "name_hint_month_count"] == 1
    assert _scope(plan, "bitget", "ETH", "candles_5m")[
        "name_hint_month_count"] == 0
    assert _scope(plan, "binance", "BTC", "candles_5m")[
        "name_hint_month_count"] == 0  # 2021 outside priority window
    assert _scope(plan, "bitget", "BTC", "candles_5m")[
        "actual_contract_listing_month"] is None


def test_real_same_venue_coin_family_six_month_hint_window():
    months = completed_range("2024-11", "2025-05")
    assert len(months) == 6
    names = [_name("BTC", "market", month) for month in months]
    # A matching Binance source and unrelated Bitget ETH month cannot
    # satisfy the six BTC/Bitget months.
    names.append(_name("ETH", "market", "2025-06"))
    plan = build_plan(summarize(names, now=NOW), max_jobs=4)
    research = [r for r in plan["dispatch_candidates"]
                if r["workflow"] == "source-native-research.yml"]
    assert len(research) == 1
    assert research[0]["inputs"] == {
        "venue": "bitget", "first_month": "2024-11", "asset": "BTC",
    }
    assert _scope(plan, "bitget", "BTC", "candles_5m")[
        "verified_complete_months_from_this_census"] == 0


def test_no_six_month_window_assembled_from_sparse_months():
    plan = build_plan(summarize([
        _name("BTC", "market", "2024-11"),
        _name("BTC", "market", "2025-01"),
        _name("BTC", "market", "2025-02"),
        _name("BTC", "market", "2025-03"),
        _name("BTC", "market", "2025-04"),
        _name("BTC", "market", "2025-05"),
    ], now=NOW))
    assert not any(t["workflow"] == "source-native-research.yml"
                   for t in plan["dispatch_candidates"])


def test_completed_range_and_training_history_not_locked_to_recent_six():
    assert completed_range("2021-01", "2021-03") == [
        "2021-01", "2021-02",
    ]
    assert select_training_months(
        now=NOW, first_month="2021-01",
    ) == ["2021-01", "2021-02", "2021-03",
          "2021-04", "2021-05", "2021-06"]
    assert select_training_months(now=NOW, first_month=None) == [
        "2026-05", "2026-06", "2026-07",
        "2026-08", "2026-09", "2026-10",
    ]
    for start in ("2020-12", "2026-06", "bad", "2024-13"):
        with pytest.raises(ValueError):
            select_training_months(now=NOW, first_month=start)


def test_workflow_cooldown_and_active_job_never_dispatch_twice():
    assert not should_dispatch([{
        "status": "in_progress", "event": "schedule",
    }], now=NOW)[0]
    assert not should_dispatch([{
        "status": "completed", "event": "workflow_dispatch",
        "created_at": "2026-11-02T11:00:00Z",
    }], now=NOW)[0]
    assert should_dispatch([{
        "status": "completed", "event": "workflow_dispatch",
        "created_at": "2026-11-01T01:00:00Z",
    }], now=NOW)[0]
    assert not should_dispatch([{
        "status": "completed", "event": "schedule",
        "created_at": "2026-11-02T12:15:00Z",
    }], now=NOW)[0]
    assert not should_dispatch([{
        "status": "completed", "event": "push",
        "created_at": "2026-11-02T13:00:00Z",
    }], now=NOW)[0]
    assert should_dispatch([{
        "status": "completed", "event": "schedule",
        "created_at": "2026-11-02T08:00:00Z",
    }], now=NOW)[0]


def test_dispatch_is_bounded_and_refuses_to_autorun_unknown_workflows():
    class Fake:
        def __init__(self):
            self.called = []
        def runs(self, workflow):
            return []
        def dispatch(self, workflow, inputs):
            self.called.append((workflow, inputs))
    plan = build_plan(summarize([], now=NOW))
    # Missing Binance core archives (issue #295) are listed first.
    assert [t["workflow"] for t in plan["dispatch_candidates"][:2]] == [
        "binance-deep-file-backfill.yml", "binance-original-auxiliary.yml",
    ]
    client = Fake()
    report = run_plan(plan, client, now=NOW)
    assert 0 < report["dispatched"] <= 3
    assert len(client.called) <= 3
    assert client.called[0] == ("binance-deep-file-backfill.yml", {})
    assert not report["overall_history_complete"]
    plan["dispatch_candidates"] = [{
        "workflow": "ci.yml", "inputs": {}, "scope": "not-authorized",
    }]
    with pytest.raises(ValueError):
        run_plan(plan, client, now=NOW)


def test_scope_floors_not_inherited_from_other_venue():
    assert ("hyperliquid", "candles_5m") not in ATTEMPT_FLOORS
    assert ATTEMPT_FLOORS[("okx", "funding")] != ATTEMPT_FLOORS[
        ("okx", "candles_5m")]


def test_native_publisher_checks_older_history_without_full_database_scan():
    recent, older = publication_ranges(NOW)
    assert recent == (
        "2026-05-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00",
    )
    assert "2021-01-01" <= older[0] < recent[0]
    assert older[1] <= recent[0]
    assert int((datetime.fromisoformat(older[1]) -
                datetime.fromisoformat(older[0])).days) in (28, 29, 30, 31)
    newer = publication_ranges(NOW.replace(hour=20))
    assert newer[0] == recent
    assert newer[1] != older
    with pytest.raises(ValueError):
        publication_ranges(datetime(2026, 11, 2))


def test_old_native_month_names_are_scoped_and_not_training_proof():
    names = [
        "hyperliquid-BTC-USDC-5m-" + month + "--" + "f" * 16 +
        ".manifest.json"
        for month in completed_range("2021-01", "2021-07")
    ]
    census = summarize(names, now=NOW)
    plan = build_plan(census, max_jobs=4)
    btc = _scope(plan, "hyperliquid", "BTC", "candles_5m")
    eth = _scope(plan, "hyperliquid", "ETH", "candles_5m")
    assert btc["all_history_named_months_not_reverified"] == 6
    assert eth["all_history_named_months_not_reverified"] == 0
    assert btc["actual_contract_listing_month"] is None
    assert btc["verified_complete_months_from_this_census"] == 0
    research = [r for r in plan["dispatch_candidates"]
                if r["workflow"] == "source-native-research.yml"]
    assert research and research[0]["inputs"] == {
        "venue": "hyperliquid", "first_month": "2021-01", "asset": "",
    }



def test_source_failure_recovery_is_bounded_and_never_retries_active_jobs():
    stale = [{"status": "completed", "conclusion": "failure",
              "event": "schedule", "created_at": "2026-11-02T01:00:00Z"}]
    assert should_recover_failed(stale, now=NOW)[0]
    assert not should_recover_failed(
        stale + [{"status": "in_progress", "event": "schedule"}], now=NOW,
    )[0]
    latest_success = stale + [{
        "status": "completed", "conclusion": "success",
        "event": "push", "created_at": "2026-11-02T11:00:00Z",
    }]
    assert not should_recover_failed(latest_success, now=NOW)[0]
    failures = [
        {"status": "completed", "conclusion": "failure",
         "event": "schedule", "created_at": f"2026-11-02T0{hour}:00:00Z"}
        for hour in (1, 2, 3)
    ]
    assert should_recover_failed(failures, now=NOW) == (
        False, "repeated_failure_requires_provider_review",
    )
    assert not should_recover_failed(stale, now=NOW.replace(hour=4))[0]


def test_coordinator_prefers_one_auto_recovery_before_optional_sampling():
    class Fake:
        def __init__(self):
            self.called = []
        def runs(self, workflow):
            if workflow == "bitget-v3-overnight-archive.yml":
                return [{"status": "completed", "conclusion": "failure",
                         "event": "schedule", "created_at": "2026-11-02T01:00:00Z"}]
            return []
        def dispatch(self, workflow, inputs):
            self.called.append((workflow, inputs))
    plan = build_plan(summarize([], now=NOW))
    client = Fake()
    report = run_plan(plan, client, now=NOW)
    assert 0 <= report["dispatched"] <= 3
    recovered = [w for w, _ in client.called
                 if w not in ("binance-deep-file-backfill.yml",
                              "binance-original-auxiliary.yml")
                 and _ == {}]
    assert len(recovered) <= 1
    assert len(client.called) <= 3


def test_core_binance_collectors_not_requested_once_archive_months_named():
    from parlay_bot.research.historical_coordinator import CORE_BINANCE_COLLECTORS
    from parlay_bot.research.history_priority import EARLIEST_ARCHIVE_MONTH

    months = completed_range(EARLIEST_ARCHIVE_MONTH, "2026-11")
    names = [f"{asset}USDT-5m-{month}--" + "a" * 16 + ".manifest.json"
             for asset in ("BTC", "ETH", "SOL", "XRP") for month in months]
    plan = build_plan(summarize(names, now=NOW))
    workflows = [t["workflow"] for t in plan["dispatch_candidates"]]
    assert "binance-deep-file-backfill.yml" not in workflows
    assert "binance-original-auxiliary.yml" in workflows
    assert set(CORE_BINANCE_COLLECTORS) == {
        "binance-deep-file-backfill.yml", "binance-original-auxiliary.yml",
    }
