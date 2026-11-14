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


async def verify(*, asset: str, root: Path, now: datetime, lookback_days: int) -> dict:
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
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(verify(
            asset=args.asset, root=args.root, now=datetime.now(UTC),
            lookback_days=args.lookback_days,
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
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print("RECORDED_PARTIAL_READBACK=" + json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
