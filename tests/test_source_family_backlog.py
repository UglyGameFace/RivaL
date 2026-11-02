"""All ten venue/family evidence backlog without pretending files are complete."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from parlay_bot.research import drive_source_census as census
from parlay_bot.research.source_family_backlog import (
    ASSETS,
    PERP_FAMILIES,
    VENUES,
    build_backlog,
)


def test_all_ten_venues_sixteen_families_and_four_assets_are_enumerated():
    report = build_backlog(census.summarize([], now=datetime(2026, 11, 2, tzinfo=UTC)))
    assert report["required_scopes"] == 10 * 4 * 16 == 640
    assert len(report["scopes"]) == 640
    assert {r["venue"] for r in report["scopes"]} == set(VENUES)
    assert {r["asset"] for r in report["scopes"]} == set(ASSETS)
    assert {r["family"] for r in report["scopes"]} == set(PERP_FAMILIES)
    assert all(not row["promoted_live"] for row in report["scopes"])
    assert all(not row["original_bytes_rehashed_in_this_report"]
               for row in report["scopes"])
    assert all("availability_unknown" in row["evidence_state"]
               for row in report["scopes"])


def test_partial_liquidity_and_funding_receipts_never_promote_a_month():
    names = [
        "gate-hourly-l2-BTC-orderbooks_slice-2026042505--"
        + "a" * 16 + ".gz.manifest.json",
        "gate-hourly-l2-BTC-orderbooks-2026042505--"
        + "b" * 16 + ".gz.manifest.json",
        "dydx-v4-SOL-USD-funding-2025-06-00-"
        + "c" * 16 + ".manifest.json",
        "lighter-XRP-mark_price-2025-06-00-"
        + "d" * 16 + ".manifest.json",
        "bitget-v3-BTC-premium-2025-12--"
        + "e" * 16 + ".manifest.json",
        "gatebulk-futures_usdt-ETH_USDT-trades-2025-03--"
        + "f" * 16 + ".manifest.json",
    ]
    report = build_backlog(census.summarize(
        names, now=datetime(2026, 11, 2, tzinfo=UTC),
    ))
    def scope(venue, asset, family):
        return next(r for r in report["scopes"] if
                    (r["venue"], r["asset"], r["family"]) ==
                    (venue, asset, family))
    gate = scope("gate", "BTC", "orderbook_l2")
    assert gate["hourly_original_depth_hints"] == 2
    assert gate["evidence_state"] == "hourly_samples_not_continuous"
    assert scope("gate", "ETH", "trades")["monthly_manifest_name_hints"] == 1
    assert scope("dydx", "SOL", "funding")["partial_source_page_scopes"] == 1
    assert scope("lighter", "XRP", "mark")["partial_source_page_scopes"] == 1
    assert scope("bitget", "BTC", "premium")["priority_lane"] == (
        "slow_independent_bitget_recovery")
    assert scope("okx", "BTC", "trades")["priority_lane"] == (
        "parallel_primary_venue")
    assert report["all_original_data_families_complete"] is False
    assert report["historical_sources_improve_forward_accuracy_verified"] is False


def test_census_cannot_claim_model_approval_or_unknown_provider_existence():
    raw = census.summarize([], now=datetime(2026, 11, 2, tzinfo=UTC))
    raw["all_source_families_2021_to_present_verified"] = True
    with pytest.raises(ValueError, match="untrusted"):
        build_backlog(raw)


def test_two_year_work_queue_is_exact_per_source_month_not_2021_aggregate():
    clock = datetime(2026, 11, 2, 13, tzinfo=UTC)
    names = [
        "bitget-v3-BTC-market-2024-10--"
        + "a" * 16 + ".manifest.json",
        "bitget-v3-BTC-market-2024-11--"
        + "b" * 16 + ".manifest.json",
        "bitget-v3-BTC-market-2026-10--"
        + "c" * 16 + ".manifest.json",
        "gate-hourly-l2-BTC-orderbooks_slice-2025" + "052505--"
        + "d" * 16 + ".gz.manifest.json",
    ]
    report = build_backlog(census.summarize(names, now=clock))
    assert report["priority_window"]["first_completed_utc_month"] == "2024-11"
    assert report["priority_window"]["last_completed_utc_month"] == "2026-10"
    assert report["priority_window"]["live_partial_utc_month"] == "2026-11"
    assert report["priority_required_venue_asset_family_month_scopes"] == 15360
    assert report["required_scopes"] == 640
    row = next(
        r for r in report["scopes"]
        if (r["venue"], r["asset"], r["family"]) == ("bitget", "BTC", "candles_5m")
    )
    assert row["priority_window_named_months_not_reverified"] == [
        "2024-11", "2026-10",
    ]
    assert row["priority_24_completed_months"] == 24
    assert len(row["priority_window_unlisted_months_availability_unknown"]) == 22
    assert "2024-10" not in row["priority_window_named_months_not_reverified"]
    assert row["original_bytes_rehashed_in_this_report"] is False
    assert row["causal_feature_import_approved"] is False
    gate = next(
        r for r in report["scopes"]
        if (r["venue"], r["asset"], r["family"]) == ("gate", "BTC", "orderbook_l2")
    )
    assert gate["priority_window_sampled_l2_hours_months_only"] == ["2025-05"]
    assert gate["priority_window_named_months_not_reverified"] == []
    assert gate["full_calendar_or_sequence_continuity_verified"] is False
