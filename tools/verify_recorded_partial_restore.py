"""Read-only, private-Drive proof of original daily-candle partition restoration.

Never publishes artifacts to RivaL Drive, uses the live database, or trains models.
Only the concise JSON receipt is safe for GitHub Actions upload; original Drive
bytes and their restored manifests remain local to the ephemeral runner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.publish import _drive
from parlay_bot.research.recorded_candles import restore_recorded_partitions


def _category(exc: Exception) -> str:
    """Non-secret failure categories; never copy raw upstream text to Actions."""
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
        "verified_native_fidelity": {
            name: item.get("status") == "verified"
            for name, item in market.get("verification", {}).items()
        },
        "train_end_utc": train_end.isoformat(),
        "test_start_utc": test_start.isoformat(),
        "trained_through_utc": trained_through.isoformat(),
        "frame_stream_count": len(frames),
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
    last = now.astimezone(UTC).date() - timedelta(days=1)
    first = last - timedelta(days=lookback_days - 1)
    drive = _drive(Settings())
    try:
        result = await restore_recorded_partitions(
            drive, asset=asset, root=root, first_day=first, last_day=last,
            now=now.astimezone(UTC),
        )
        checks = []
        for manifest_path in result["restored"]:
            audit = audit_partition(Path(manifest_path))
            if audit["full_calendar_month_verified"]:
                raise ValueError("partial ledger cannot claim complete original source")
            checks.append({
                "venue": audit["venue"], "contract": audit["contract"],
                "month": audit["month"], "rows": audit["rows"],
                "full_calendar_month_verified": False,
            })
        admission = (await verify_xmarket_admission(
            drive, asset=asset, root=root, now=now,
        )) if checks and xmarket_admission else None
        return {
            "schema_version": "rival-partial-candle-readback-v1",
            "asset": asset, "as_of_utc": now.astimezone(UTC).isoformat(),
            "first_original_day": first.isoformat(), "last_original_day": last.isoformat(),
            "status": "verified_original_readback" if checks else "no_eligible_originals",
            "source_backup_days": len(result["source_files"]),
            "missing_backup_days": len(result["missing_backup_days"]),
            "partitions": checks,
            "unknown_trade_count": sum(
                p["unknown_trade_count"] for p in result["partitions"]
            ),
            "original_sha_readback_checked": bool(checks),
            **({"xmarket_admission": admission} if admission is not None else {}),
            "trained_model": False, "eligible_historical_splits_proven": False,
            "private_drive_modified": False, "production_database_modified": False,
        }
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only original candle restore evidence")
    parser.add_argument("--asset", choices=("BTC", "ETH", "SOL", "XRP"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lookback-days", type=int, default=28)
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
