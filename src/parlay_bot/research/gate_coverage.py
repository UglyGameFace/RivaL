"""Evidence-based Gate source-month coverage ledger, never projected completeness.

The ledger is deliberately distinct from a certified model import. A manifest
name is only an inventory hint, not a successful download or SHA readback in
this run. Source-native model qualification still requires the strict restore
and zero-gap checks in the existing native-research code.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime


def build_gate_coverage(
    *, months: list[str], assets: tuple[str, ...], kinds: tuple[str, ...],
    existing: dict[tuple[str, str, str], str],
    native_months: set[tuple[str, str]], result: dict,
) -> dict:
    if (len(months) > 100 or len(assets) > 4 or len(kinds) > 7
        or len(set(months)) != len(months)):
        raise ValueError("Gate ledger scope exceeds source inventory")
    downloaded = {
        (item["asset"], item["kind"], item["month"])
        for item in result.get("published", [])
    }
    reverified = {
        (item["asset"], item["kind"], item["month"])
        for item in result.get("reverified_existing", [])
    }
    native_new = {
        (item["asset"], item["month"])
        for item in result.get("native_qualified", [])
    }
    failed = {
        (item["asset"], item["kind"], item["month"]): item["reason"]
        for item in result.get("unavailable_or_rejected", [])
    }
    blocked = {
        (item["asset"], item["month"]): item["reason"]
        for item in result.get("native_blockers", [])
    }
    api = {
        (item["asset"], item["month"]): item
        for item in result.get("same_venue_api_fallback", [])
    }
    gap = {
        (item["asset"], item["month"]): item
        for item in result.get("five_minute_source_gap_inventory", [])
    }
    # Defensive: no untrusted selected-job string can be treated as a
    # verified source, and no local success implies future skill.
    rows = []
    for month in months:
        for asset in assets:
            for kind in kinds:
                key = (asset, kind, month)
                if key in downloaded:
                    raw = "downloaded_and_drive_sha_readback_this_run"
                elif key in reverified:
                    raw = "existing_original_reverified_drive_sha_this_run"
                elif key in existing:
                    raw = "remote_manifest_name_only_not_reverified_this_run"
                elif key in failed:
                    reason = failed[key].lower()
                    if "http 404" in reason:
                        raw = "provider_archive_http_404"
                    elif any(f"http {status}" in reason for status in
                             ("429", "502", "503", "504")):
                        raw = "provider_throttled_or_unavailable"
                    else:
                        raw = "download_or_schema_rejected"
                else:
                    raw = "not_downloaded_and_not_attempted_this_run"
                row = {
                    "venue": "gate", "asset": asset,
                    "contract": f"{asset}_USDT", "quote": "USDT",
                    "interval_minutes": (
                        5 if kind == "candlesticks_5m" else None
                    ),
                    "data_family": kind, "utc_month": month,
                    "raw_status": raw,
                    "original_gate_gzip_verified_this_run": key in downloaded or key in reverified,
                    "native_training_eligible_this_run": False,
                    "native_proof": "not_applicable_to_non_5m_candles",
                }
                if kind == "candlesticks_5m":
                    ident = (asset, month)
                    if ident in native_new:
                        row["native_proof"] = "qualified_and_drive_verified_this_run"
                        row["native_training_eligible_this_run"] = True
                    elif ident in blocked:
                        row["native_proof"] = "blocked_by_source_evidence"
                        row["native_blocker"] = blocked[ident]
                    elif ident in native_months:
                        row["native_proof"] = "remote_native_manifest_listed_not_restored"
                    else:
                        row["native_proof"] = "no_qualified_native_month"
                    if ident in gap:
                        evidence = gap[ident]
                        row["raw_missing_5m_candles"] = evidence["missing_five_minute_slots"]
                        row["raw_candle_gap_list_truncated"] = (
                            evidence.get("gap_timestamp_list_truncated", False)
                        )
                        row["raw_missing_utc_timestamps"] = evidence.get(
                            "missing_utc_timestamps", []
                        )
                    if ident in api:
                        item = api[ident]
                        row["api_fallback_status"] = item["status"]
                        row["api_research_only"] = True
                        row["api_evidence_sha_readback_this_run"] = (
                            item["status"] == "api_complete_research_only"
                            and item.get("source_api_response_readback_verified") is True
                        )
                rows.append(row)
    expected = len(months) * len(assets) * len(kinds)
    assert len(rows) == expected
    raw_counts = dict(sorted(Counter(row["raw_status"] for row in rows).items()))
    native_counts = dict(sorted(Counter(
        row["native_proof"] for row in rows
        if row["data_family"] == "candlesticks_5m"
    ).items()))
    return {
        "schema_version": "rival-gate-source-month-ledger-v1",
        "recorded_utc": datetime.now(UTC).isoformat(),
        "source": "gate_futures_usdt",
        "source_calendar_starts": months[0] if months else None,
        "last_complete_utc_month": months[-1] if months else None,
        "assets": list(assets), "data_families": list(kinds),
        "expected_venue_asset_family_months": expected,
        "inventory_count_not_proof_of_verified_history": len(existing),
        "source_month_status_counts": raw_counts,
        "native_5m_status_counts": native_counts,
        "genuine_forward_predictions_recorded_by_this_job": 0,
        "approved_live_models": 0,
        "full_history_verified": False,
        "rows": rows,
    }
