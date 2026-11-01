"""Conservative source-month evidence reporting for Gate's real Drive warehouse."""
from __future__ import annotations

import pytest

from parlay_bot.research.gate_coverage import build_gate_coverage


def test_month_ledger_distinguishes_immutable_bytes_from_remote_name_hints():
    existing = {
        ("BTC", "candlesticks_5m", "2026-09"): "a" * 16,
        ("BTC", "candlesticks_5m", "2026-10"): "b" * 16,
        ("ETH", "candlesticks_5m", "2026-10"): "c" * 16,
        ("BTC", "mark_prices", "2026-10"): "d" * 16,
    }
    result = {
        "published": [{
            "asset": "BTC", "kind": "candlesticks_5m", "month": "2026-10",
        }],
        "reverified_existing": [{
            "asset": "BTC", "kind": "mark_prices", "month": "2026-10",
        }],
        "native_qualified": [{"asset": "BTC", "month": "2026-10"}],
        "native_blockers": [{
            "asset": "ETH", "month": "2026-10",
            "reason": "historical contract multiplier changed",
        }],
        "five_minute_source_gap_inventory": [{
            "asset": "BTC", "month": "2026-10",
            "missing_five_minute_slots": 0,
            "gap_timestamp_list_truncated": False,
            "missing_utc_timestamps": [],
        }],
        "same_venue_api_fallback": [{
            "asset": "ETH", "month": "2026-09",
            "status": "api_complete_research_only",
            "source_api_response_readback_verified": True,
        }],
        "unavailable_or_rejected": [
            {"asset": "ETH", "kind": "candlesticks_5m", "month": "2026-09",
             "reason": "Gate public archive unavailable: HTTP 404"},
            {"asset": "ETH", "kind": "mark_prices", "month": "2026-10",
             "reason": "Gate public archive unavailable: HTTP 429"},
        ],
    }
    report = build_gate_coverage(
        months=["2026-09", "2026-10"],
        assets=("BTC", "ETH"), kinds=("candlesticks_5m", "mark_prices"),
        existing=existing, native_months={("BTC", "2026-09")},
        result=result,
    )
    assert report["expected_venue_asset_family_months"] == 8
    assert report["full_history_verified"] is False
    assert report["approved_live_models"] == 0
    records = {(x["asset"], x["data_family"], x["utc_month"]): x
               for x in report["rows"]}
    assert records[("BTC", "candlesticks_5m", "2026-10")]["native_proof"] == (
        "qualified_and_drive_verified_this_run"
    )
    assert records[("BTC", "candlesticks_5m", "2026-09")]["native_proof"] == (
        "remote_native_manifest_listed_not_restored"
    )
    assert records[("BTC", "candlesticks_5m", "2026-09")][
        "original_gate_gzip_verified_this_run"
    ] is False
    assert records[("ETH", "candlesticks_5m", "2026-10")][
        "native_training_eligible_this_run"
    ] is False
    assert records[("ETH", "candlesticks_5m", "2026-10")][
        "native_blocker"
    ] == "historical contract multiplier changed"
    assert records[("BTC", "mark_prices", "2026-10")]["raw_status"] == (
        "existing_original_reverified_drive_sha_this_run"
    )
    assert records[("ETH", "candlesticks_5m", "2026-09")][
        "api_evidence_sha_readback_this_run"
    ] is True
    assert records[("ETH", "candlesticks_5m", "2026-09")][
        "native_training_eligible_this_run"
    ] is False
    assert records[("ETH", "mark_prices", "2026-10")]["raw_status"] == (
        "provider_throttled_or_unavailable"
    )
    assert records[("ETH", "mark_prices", "2026-09")]["raw_status"] == (
        "not_downloaded_and_not_attempted_this_run"
    )
    assert records[("BTC", "candlesticks_5m", "2026-10")][
        "raw_missing_5m_candles"
    ] == 0


def test_full_inventory_does_not_imply_six_month_model_approval():
    months = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    rows = {
        ("BTC", "candlesticks_5m", month): "f" * 16 for month in months
    }
    report = build_gate_coverage(
        months=months, assets=("BTC",), kinds=("candlesticks_5m",),
        existing=rows, native_months={("BTC", month) for month in months},
        result={},
    )
    assert report["full_history_verified"] is False
    assert report["genuine_forward_predictions_recorded_by_this_job"] == 0
    assert report["approved_live_models"] == 0
    assert all(x["native_proof"] == "remote_native_manifest_listed_not_restored"
               for x in report["rows"])


def test_ledger_rejects_unbounded_or_duplicate_month_scope():
    with pytest.raises(ValueError):
        build_gate_coverage(
            months=["2026-09", "2026-09"], assets=("BTC",),
            kinds=("candlesticks_5m",), existing={}, native_months=set(),
            result={},
        )
    with pytest.raises(ValueError):
        build_gate_coverage(
            months=["2026-01"] * 102, assets=("BTC",),
            kinds=("candlesticks_5m",), existing={}, native_months=set(),
            result={},
        )
