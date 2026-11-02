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
from parlay_bot.research.source_native_offline import select_training_months
from tools.historical_dispatch import run_plan, should_dispatch

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


def test_dispatch_is_bounded_and_refuses_to_autorun_unknown_workflows():
    class Fake:
        def __init__(self):
            self.called = []
        def runs(self, workflow):
            return []
        def dispatch(self, workflow, inputs):
            self.called.append((workflow, inputs))
    plan = build_plan(summarize([], now=NOW))
    client = Fake()
    report = run_plan(plan, client, now=NOW)
    assert 0 < report["dispatched"] <= 2
    assert len(client.called) <= 2
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
