"""Read-only, private-Drive proof of original daily-candle partition restoration.

Never publishes artifacts to RivaL Drive, uses the live database, or trains models.
Only the concise JSON receipt is safe for GitHub Actions upload; original Drive
bytes and their restored manifests remain local to the ephemeral runner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.publish import _drive
from parlay_bot.research.recorded_candles import restore_recorded_partitions

# PR #633 single-family OKX pin, matching research_after_source
# (tools/restore_archive_months.py --okx-source-integrity). The pin filter
# in restore_verified_source_months runs before its conflict check, so an
# unpinned restore fails closed on any divergent OKX original while the
# pinned restore skips the rival family. This selects one documented
# family; it never picks a favourable archive or relaxes any gate.
OKX_SOURCE_INTEGRITY = "okx-v5-public-history-5m-v1"

# Only these already-public, credential-free conflict comparison fields
# may enter the receipt. Raw manifests, prices and Drive IDs stay local.
_CONFLICT_COMPARISON_FIELDS = (
    "first_rows", "second_rows", "all_candle_fields_identical",
    "differing_fields", "price_fields_differ", "volume_fields_differ",
    "trade_count_differs", "grid_or_identity_fields_differ",
    "semantic_fields_equal", "identical_except_first_observed_at",
)


def _source_conflict_receipt(evidence: dict) -> dict:
    """Trim an OKX source-conflict to its month and differing fields."""
    comparison = evidence.get("comparison") or {}
    return {
        "month": evidence.get("month"),
        "comparison": {
            key: comparison.get(key) for key in _CONFLICT_COMPARISON_FIELDS
        },
    }


# ID/SHA-shaped runs are never receipt-safe; the warehouse reader's own
# failure strings are fixed literals with no such runs, so redacting the
# runs keeps those diagnostics intact while upholding the receipt contract
# (no private IDs, paths or digests beyond the 16-hex name convention).
_UNSAFE_DETAIL_RUN = re.compile(r"[A-Za-z0-9_-]{24,}")


def _safe_detail(detail: object) -> str:
    """Bounded exclusion detail with ID/SHA-shaped runs redacted."""
    return _UNSAFE_DETAIL_RUN.sub("<redacted>", str(detail))[:200]


def _bundle_diagnostic(bundle: dict) -> dict:
    """Receipt-safe shape of one verified bundle: identity and origin mix.

    Distinguishes the bot's dated prospective exports (``day`` set) from the
    collector's backfill chunks and progress journals, and counts samples by
    origin so a missing live side is attributable to a specific bundle set.
    """
    samples = bundle.get("samples") or []
    live = sum(1 for row in samples if row.get("origin") == "live")
    backfill = sum(1 for row in samples if row.get("origin") == "backfill")
    return {
        "sha256_16": str(bundle.get("_bundle_sha256") or "")[:16],
        "day": bundle.get("day"),
        "backfill_checkpoint": bool(bundle.get("backfill_checkpoint")),
        "created_at": bundle.get("created_at"),
        "samples": len(samples),
        "live_samples": live,
        "backfill_samples": backfill,
        "other_origin_samples": len(samples) - live - backfill,
    }


def _category(exc: Exception) -> str:
    """Non-secret failure categories; never copy raw upstream text to Actions."""
    if isinstance(exc, ModuleNotFoundError):
        return ("missing_xmarket_runner_dependency"
                if exc.name in {"pandas", "numpy", "duckdb"}
                else "missing_runner_module")
    detail = str(exc)
    for phrase, code in (
        ("invalid recorded candle price geometry", "invalid_price_geometry"),
        ("invalid recorded candle volume", "invalid_volume"),
        ("invalid recorded candle trade count", "invalid_trade_count"),
        ("recorded candle grid/observation clock mismatch", "invalid_clock"),
        ("unknown trade-count evidence mismatch", "invalid_trade_provenance"),
        ("conflicting recorded candle observations", "conflicting_original"),
        ("invalid recorded source", "invalid_source"),
    ):
        if phrase in detail:
            return code
    return "unclassified_restoration_failure"


async def verify_xmarket_admission(drive, *, asset: str, root: Path,
                                    now: datetime) -> dict:
    """Read *existing* verified source bundles and run production frame admission.

    This never invokes the backfill collector: it performs no external
    contract requests, gap retries, Drive writes, model fits or publication.
    The native report is intentionally summarized instead of uploading raw
    contract identifiers, account file IDs, observed prices or model weights.
    """
    import hashlib

    from parlay_bot.research.archive_training import load_frames_manifests
    from parlay_bot.research.xmarket_history import (
        BUNDLE_PREFIX,
        combine,
        validate_bundle,
    )
    from parlay_bot.research.xmarket_training import train_run

    frames, provenance = load_frames_manifests(root, target_asset=asset)
    bundles, cursor = [], None
    for page in range(32):
        files, cursor = await drive.read_verified_research_reports(
            limit=20, page_token=cursor, return_next_page=True,
            name_prefix=BUNDLE_PREFIX + asset + "-",
        )
        for file_id, raw in files:
            doc = validate_bundle(json.loads(raw))
            bundles.append({
                **doc, "_drive_file_id": file_id,
                "_bundle_sha256": hashlib.sha256(raw).hexdigest(),
            })
        if cursor is None:
            break
    else:
        raise ValueError("XMarket readback exceeds bounded verified report inventory")
    if not bundles:
        return {
            "status": "no_verified_native_source_bundles",
            "verified_source_bundles": 0, "frame_stream_count": len(frames),
            "training_or_model_published": False,
        }

    market = combine(bundles)
    # Timestamps and counts distinguish absent native observations from
    # missing candle archives without exposing source contracts or prices.
    from parlay_bot.research.xmarket_features import timestamp

    native_windows = {}
    for source in ("polymarket", "kalshi"):
        clocks = [timestamp(row["at"]) for row in market["samples"]
                  if row["source"] == source]
        native_windows[source] = {
            "points": len(clocks),
            "first_utc": min(clocks).isoformat() if clocks else None,
            "last_utc": max(clocks).isoformat() if clocks else None,
        }
    window_start = now.astimezone(UTC).date()
    train_end = datetime.combine(
        window_start - timedelta(days=56), datetime.min.time(), tzinfo=UTC
    )
    test_start = datetime.combine(
        window_start - timedelta(days=28), datetime.min.time(), tzinfo=UTC
    )
    trained_through = datetime.combine(
        window_start - timedelta(days=1),
        datetime.min.time(), tzinfo=UTC,
    ) + timedelta(hours=23, minutes=55)
    reports = {}
    for horizon in (5, 15, 60):
        result = train_run(
            frames, asset=asset, horizon=horizon,
            train_end=train_end, test_start=test_start,
            trained_through=trained_through, provenance=provenance,
            code_version="read-only-original-admission",
            market_data={**market, "asset": asset},
            admission_only=True,
        )
        if result["artifact"] is not None:
            raise ValueError("read-only admission unexpectedly produced a fitted artifact")
        report = result["report"]
        streams = report.get("row_admission", {})
        overlap = report.get("overlap_evidence", {})
        split = overlap.get("splits", {})
        reports[str(horizon)] = {
            "status": report.get("training_status", "blocked"),
            "input_features": report.get("input_count"),
            "selected_dispersion_venues": report.get("dispersion_venues", []),
            "native_exact_source_pairs": {
                name: info.get("all_sources_rows", 0)
                for name, info in overlap.get("by_stream", {}).items()
            },
            "complete_source_and_label_rows": {
                name: info.get("complete_rows", 0)
                for name, info in streams.items()
            },
            "effective_joined_rows": report.get("effective_window", {}).get("rows", 0),
            "split_rows": {
                name: {
                    "rows": section.get("rows", 0),
                    "shortfall": section.get("shortfall", floor),
                    "unique_bars": section.get("unique_bars", 0),
                } for name, floor in (("train", 300), ("validation", 100), ("test", 100))
                for section in (split.get(name, {}),)
            },
            "split_eligible": bool(overlap.get("eligible", False)),
            "model_fitted": False, "model_published": False,
        }
    return {
        "status": ("exact_features_joined"
                   if any(r["effective_joined_rows"] > 0 for r in reports.values())
                   else "no_complete_source_and_label_rows"),
        "verified_source_bundles": len(bundles),
        "bundle_diagnostics": [_bundle_diagnostic(b) for b in bundles],
        "verified_native_fidelity": {
            name: item.get("status") == "verified"
            for name, item in market.get("verification", {}).items()
        },
        # Step 6 publishes these per-source diagnostics; step 8 previously
        # discarded them, so a fidelity flip between the two steps could not
        # be classified from this artifact. Receipt-only: no gate changes.
        "native_source_fidelity": {
            name: {
                "status": item.get("status"),
                "blocked_reason": item.get("blocked_reason"),
                "paired_points": item.get("paired_points"),
                "overlap_coverage": item.get("overlap_coverage"),
                "mismatch_rate": item.get("mismatch_rate"),
            }
            for name, item in market.get("verification", {}).items()
        },
        "train_end_utc": train_end.isoformat(),
        "test_start_utc": test_start.isoformat(),
        "trained_through_utc": trained_through.isoformat(),
        "frame_stream_count": len(frames),
        "candle_frame_windows": {
            stream: {
                "rows": len(candles),
                "first_utc": candles.index.min().isoformat() if len(candles) else None,
                "last_utc": candles.index.max().isoformat() if len(candles) else None,
            } for stream, (candles, _features) in frames.items()
        },
        "native_source_windows": native_windows,
        "horizons": reports,
        "training_or_model_published": False,
    }


async def verify(*, asset: str, root: Path, now: datetime, lookback_days: int,
                 xmarket_admission: bool = False) -> dict:
    """Restore original bytes into a private ephemeral path and prove every row."""
    if asset not in {"BTC", "ETH", "SOL", "XRP"} or now.utcoffset() is None:
        raise ValueError("invalid source verification scope")
    if not 1 <= lookback_days <= 90:
        raise ValueError("lookback_days must be 1..90")
    if xmarket_admission and lookback_days <= 56:
        raise ValueError("XMarket admission must inspect all three chronological windows")
    last = now.astimezone(UTC).date() - timedelta(days=1)
    first = last - timedelta(days=lookback_days - 1)
    drive = _drive(Settings())
    try:
        restoration = None
        if xmarket_admission:
            from parlay_bot.research.archive_training import restore_verified_months
            from parlay_bot.research.batch import complete_months

            # Match the production trainer: six verified complete months,
            # then the original daily ledgers for missing/partial months.
            # The former 28-day ledger-only check could never inspect its
            # own training (< now-56d) or validation (< now-28d) windows.
            restoration = await restore_verified_months(
                asset=asset, months=complete_months(now=now, count=6), root=root,
                records_since=first, records_through=last,
                now=now.astimezone(UTC), drive=drive,
                okx_source_integrity=OKX_SOURCE_INTEGRITY,
            )
            result = restoration.get("recorded_candles") or {
                "restored": [], "source_files": [], "partitions": [],
                "missing_backup_days": [],
            }
            paths = sorted(root.rglob("*.manifest.json"))
        else:
            result = await restore_recorded_partitions(
                drive, asset=asset, root=root, first_day=first, last_day=last,
                now=now.astimezone(UTC),
            )
            paths = [Path(p) for p in result["restored"]]
        checks = []
        for manifest_path in paths:
            audit = audit_partition(manifest_path)
            if audit["full_calendar_month_verified"] and not xmarket_admission:
                raise ValueError("partial ledger cannot claim complete original source")
            checks.append({
                "venue": audit["venue"], "contract": audit["contract"],
                "month": audit["month"], "rows": audit["rows"],
                "full_calendar_month_verified": audit["full_calendar_month_verified"],
            })
        admission = (await verify_xmarket_admission(
            drive, asset=asset, root=root, now=now,
        )) if checks and xmarket_admission else None
        split_proven = bool(
            admission and admission.get("status") == "exact_features_joined"
            and all(admission.get("verified_native_fidelity", {}).get(source) is True
                    for source in ("polymarket", "kalshi"))
            and all(admission.get("horizons", {}).get(str(horizon), {}).get("split_eligible")
                    is True for horizon in (5, 15, 60))
        )
        return {
            "schema_version": "rival-partial-candle-readback-v1",
            "asset": asset, "as_of_utc": now.astimezone(UTC).isoformat(),
            "first_original_day": first.isoformat(), "last_original_day": last.isoformat(),
            "status": "verified_original_readback" if checks else "no_eligible_originals",
            "source_backup_days": len(result["source_files"]),
            "missing_backup_days": len(result["missing_backup_days"]),
            "recorded_backup_inventory_available": restoration is None or (
                "recorded_candles" in restoration
            ),
            "partitions": checks,
            "unknown_trade_count": sum(
                p["unknown_trade_count"] for p in result["partitions"]
            ),
            "original_sha_readback_checked": bool(checks),
            **({"xmarket_admission": admission} if admission is not None else {}),
            **({"input_restoration": {
                "mode": "production_verified_months_and_recorded_partitions",
                "months_requested": restoration["months_requested"],
                "okx_selected_source_integrity": restoration.get(
                    "okx_selected_source_integrity"),
                "restored_manifest_counts": {
                    source: len(items) for source, items in restoration["restored"].items()
                },
                # Upstream details include private IDs/paths. Publish only
                # source names and exception class; keep the raw report local.
                "excluded_reason_classes": {
                    source: item["reason"] for source, item in restoration["excluded"].items()
                },
                # The recorded-partitions leg is the one exception: its
                # reader raises fixed diagnostic literals, so a redacted
                # detail names the failure without exposing IDs or paths.
                "excluded_reason_details": {
                    source: _safe_detail(item["detail"])
                    for source, item in restoration["excluded"].items()
                    if source == "recorded-partial-partitions"
                    and isinstance(item, dict) and item.get("detail")
                },
                # Conflict month/differing fields only; the full evidence
                # (manifest SHAs, no prices or IDs) stays in the local report.
                "excluded_source_conflicts": {
                    source: _source_conflict_receipt(item["source_conflict"])
                    for source, item in restoration["excluded"].items()
                    if isinstance(item, dict)
                    and isinstance(item.get("source_conflict"), dict)
                },
                "historical_split_status": "eligible" if split_proven else "blocked",
            }} if restoration is not None else {}),
            "trained_model": False, "eligible_historical_splits_proven": split_proven,
            "private_drive_modified": False, "production_database_modified": False,
        }
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only original candle restore evidence")
    parser.add_argument("--asset", choices=("BTC", "ETH", "SOL", "XRP"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--xmarket-admission", action="store_true",
                        help="Read only verified native bundles and report actual feature/split admission")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(verify(
            asset=args.asset, root=args.root, now=datetime.now(UTC),
            lookback_days=args.lookback_days,
            xmarket_admission=args.xmarket_admission,
        ))
    except Exception as exc:  # noqa: BLE001 - preserve failure, never expose private payloads
        report = {
            "schema_version": "rival-partial-candle-readback-v1",
            "asset": args.asset, "status": "restoration_failed",
            "error_class": type(exc).__name__, "error_category": _category(exc),
            "original_sha_readback_checked": False,
            "trained_model": False, "eligible_historical_splits_proven": False,
            "private_drive_modified": False, "production_database_modified": False,
        }
        exit_code = 1
    else:
        exit_code = 0 if report["status"] == "verified_original_readback" else 2
        if args.xmarket_admission and exit_code == 0:
            evidence = report.get("xmarket_admission", {})
            if evidence.get("status") != "exact_features_joined":
                exit_code = 3
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print("RECORDED_PARTIAL_READBACK=" + json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
