"""Ten-venue source-family acquisition backlog from untrusted Drive NAME hints.

This is a prioritization ledger, never an assertion about source availability,
contract listing dates, verified original bytes or model readiness.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from parlay_bot.research.source_family_coverage import (
    ASSETS, PERP_FAMILIES, VENUES,
)

FAMILY_ALIASES = {
    ("bitget", "market"): "candles_5m",
    ("bitget", "mark"): "mark",
    ("bitget", "index"): "index",
    ("bitget", "premium"): "premium",
    ("gate", "candlesticks_1m"): "candles_1m",
    ("gate", "candlesticks_5m"): "candles_5m",
    ("gate", "candlesticks_1h"): "candles_1h",
    ("gate", "native_candles_5m"): "candles_5m",
    ("gate", "mark_prices"): "mark",
    ("gate", "funding_applies"): "funding",
    ("gate", "funding_updates"): "funding",
    ("gate", "trades"): "trades",
    ("dydx", "funding_with_oracle_price"): "funding",
    ("lighter", "mark_price"): "mark",
}
SOURCE_MONTH_STARTS_ARE_NOT_LISTING_PROOF = True


def family_for(venue: str, family: str) -> str | None:
    canonical = FAMILY_ALIASES.get((venue, family), family)
    return canonical if canonical in PERP_FAMILIES else None


def build_backlog(census: dict) -> dict:
    """All 640 required venue/asset/family scopes, including absent hints."""
    if (census.get("schema_version") != "rival-drive-source-manifest-census-v1"
        or census.get("all_source_families_2021_to_present_verified") is not False):
        raise ValueError("cannot plan from untrusted or certified-looking census")
    hints: dict[tuple[str, str, str], Counter] = {}
    def add(row: dict, field: str, count: int) -> None:
        venue, asset, source_family = (
            row.get("venue"), row.get("asset"), row.get("family"))
        if venue not in VENUES or asset not in ASSETS:
            raise ValueError("unexpected warehouse source identity")
        family = family_for(venue, source_family)
        if family is None or count <= 0:
            return
        scope = (venue, asset, family)
        hints.setdefault(scope, Counter())[field] += count

    for r in census.get("month_scopes", []):
        add(r, "monthly_manifest_names", int(r.get("listed_manifest_months") or 0))
    for r in census.get("other_family_name_counts", []):
        add(r, "monthly_manifest_names", int(r.get("listed_months") or 0))
    for r in census.get("page_level_sources_not_full_months", []):
        add(r, "partial_source_page_scopes", 1)
    for r in census.get("gate_hourly_l2_not_full_months", []):
        add({**r, "family": "orderbook_l2"},
            "sampled_hourly_depth_files", int(r.get("distinct_named_hours") or 0))
    unclassified_okx = {
        asset: 0 for asset in ASSETS
    }
    for r in census.get("okx_auxiliary_receipt_name_hints", []):
        if r.get("venue") != "okx" or r.get("asset") not in ASSETS:
            raise ValueError("unknown OKX receipt identity")
        unclassified_okx[r["asset"]] += int(
            r.get("distinct_original_filenames") or 0
        )

    rows, totals = [], {}
    for venue in VENUES:
        counter = Counter()
        for asset in ASSETS:
            for family in PERP_FAMILIES:
                evidence = hints.get((venue, asset, family), Counter())
                monthly = evidence["monthly_manifest_names"]
                pages = evidence["partial_source_page_scopes"]
                hours = evidence["sampled_hourly_depth_files"]
                state = (
                    "monthly_manifest_names_not_byte_verified" if monthly
                    else "partial_source_page_names_only" if pages
                    else "hourly_samples_not_continuous" if hours
                    else "not_indexed_in_this_census_availability_unknown"
                )
                # Important: a missing month receipt is NOT a failed HTTP
                # fetch, proof of a launch date, or published source absence.
                # Keep Bitget from monopolizing per-venue research resources.
                lane = ("slow_independent_bitget_recovery"
                        if venue == "bitget" else "parallel_primary_venue")
                row = {
                    "venue": venue, "asset": asset, "family": family,
                    "evidence_state": state, "priority_lane": lane,
                    "monthly_manifest_name_hints": monthly,
                    "partial_source_page_scopes": pages,
                    "hourly_original_depth_hints": hours,
                    "unclassified_okx_auxiliary_receipts": (
                        unclassified_okx[asset] if venue == "okx" else 0),
                    "availability_and_listing_date_verified": False,
                    "original_bytes_rehashed_in_this_report": False,
                    "full_calendar_or_sequence_continuity_verified": False,
                    "causal_feature_import_approved": False,
                    "promoted_live": False,
                }
                rows.append(row)
                counter[state] += 1
        totals[venue] = dict(sorted(counter.items()))
    return {
        "schema_version": "rival-ten-venue-family-backlog-v1",
        "source_census_generated_at": census.get("generated_at"),
        "venues": list(VENUES), "assets": list(ASSETS),
        "families": list(PERP_FAMILIES), "required_scopes": len(rows),
        "source_name_inventory_count": census.get("all_manifest_objects_listed"),
        "priority": (
            "Continue authenticated original providers for nine venues "
            "in parallel; keep Bitget oldest or unavailable historical "
            "families in a bounded independent slow lane. Never use other "
            "venue prices or order books to fill missing Bitget originals."
        ),
        "venue_status_counts": totals, "scopes": rows,
        "all_original_data_families_complete": False,
        "live_liquidity_model_admission_verified": False,
        "historical_sources_improve_forward_accuracy_verified": False,
        "warning": (
            "This is a manifest-name census, not an audit of raw contents. "
            "No-name means unindexed/unknown, not unavailable. Order-book "
            "hours and pages are not contiguous months; provider-specific "
            "contract listing dates and units require independent proof."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ten-venue family backlog ledger")
    parser.add_argument("--census", type=Path, default=Path(
        "data/native-reports/drive-source-census.json"))
    parser.add_argument("--out", type=Path, default=Path(
        "data/native-reports/drive-source-family-backlog.json"))
    args = parser.parse_args(argv)
    census = json.loads(args.census.read_text(encoding="utf-8"))
    report = build_backlog(census)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "required_scopes": report["required_scopes"],
        "venues": len(report["venues"]),
        "model_feature_admitted": False,
        "report": str(args.out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
