"""Small, resumable ORIGINAL Gate hourly L2 samples, across real calendar years.

Source authentic gzip/SHA readback only. One sampled hour does not certify a
month of L2, a quote history, contract listing or a future-only prediction.
Batches are bounded to preserve the owner's free quota and other collectors.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.drive_source_census import list_names
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_hourly_l2_archive import (
    ASSETS, FAMILIES, FIRST_ATTEMPT, download, hour_bounds, store,
)
from parlay_bot.research.publish import _drive

MAX_ATTEMPTS = 12
MAX_BATCH_SOURCE_BYTES = 32 * 1024 * 1024
WINDOW_HOURS = 6
# Retain two independent original formats. One source snapshot is not a
# substitute for a linked change-log and vice versa.
GATE_L2_MANIFEST = re.compile(
    r"gate-hourly-l2-(BTC|ETH|SOL|XRP)-(orderbooks|orderbooks_slice)-"
    r"(20[0-9]{8})--[a-f0-9]{16}\.gz\.manifest\.json"
)


def existing_hours(names: list[str]) -> set[tuple[str, str, str]]:
    """Manifest names are *skip hints*, NOT new verification or certification."""
    seen = set()
    for name in names:
        match = GATE_L2_MANIFEST.fullmatch(name)
        if match:
            seen.add((match.group(1), match.group(2), match.group(3)))
    return seen


def historical_month_anchors(now: datetime) -> list[str]:
    """Month-stratified hour attempts, latest first then older 2021+ years.

    25th UTC at 05:00 was *directly observed* in April 2023, but the
    same calendar time elsewhere is just a candidate, not listing proof.
    """
    if now.utcoffset() is None:
        raise HistoryImportError("timezone-aware archive clock required")
    clock = now.astimezone(UTC)
    start = clock.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    dates = []
    while start > FIRST_ATTEMPT:
        start = (start - timedelta(days=1)).replace(day=1)
        hour = start.replace(day=25, hour=5)
        if hour + timedelta(hours=1) <= clock and hour >= FIRST_ATTEMPT:
            dates.append(hour.strftime("%Y%m%d%H"))
        if len(dates) > 100:
            raise HistoryImportError("Gate historical month inventory unexpectedly large")
    return dates


def candidate_slots(now: datetime) -> list[tuple[str, str, str]]:
    """BTC/ETH first for listing-independent original validation; all four probed.

    Keep older source attempts even where current exchange APIs cannot query
    that date. No rows are fabricated for a not-yet-listed contract.
    """
    hours = historical_month_anchors(now)
    return [
        (asset, family, hour)
        for hour in hours
        for family in ("orderbooks_slice", "orderbooks")
        for asset in ASSETS
    ]


def select_slots(
    now: datetime, listed: set[tuple[str, str, str]], *,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[list[tuple[str, str, str]], int]:
    """Deterministic six-hour rotation, not a restart-at-first-month loop."""
    if now.utcoffset() is None or not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise HistoryImportError("invalid Gate L2 auto collection budget")
    slots = candidate_slots(now)
    if not slots:
        return [], 0
    rotation = int(now.timestamp()) // (WINDOW_HOURS * 3600)
    start = (rotation * max_attempts) % len(slots)
    chosen = []
    skipped = 0
    for i in range(len(slots)):
        slot = slots[(start + i) % len(slots)]
        if slot in listed:
            skipped += 1
            continue
        chosen.append(slot)
        if len(chosen) == max_attempts:
            break
    return chosen, skipped


async def collect_batch(
    *, now: datetime, root: Path, report_path: Path,
    max_attempts: int = MAX_ATTEMPTS,
    selected_hour: str | None = None,
) -> dict:
    """Archive at most 12 source files, serially, with Drive original readback.

    Never replace the existing per-asset/page collectors or the production bot.
    """
    drive = _drive(Settings())
    try:
        names = await list_names(drive)
        listed = existing_hours(names)
        if selected_hour:
            hour_bounds(selected_hour, now=now)
            # One manual UTC hour × two original families × four assets.
            candidates = [(asset, family, selected_hour)
                          for family in FAMILIES for asset in ASSETS]
            slots = [slot for slot in candidates if slot not in listed]
            skipped = len(candidates) - len(slots)
            slots = slots[:max_attempts]
        else:
            slots, skipped = select_slots(
                now, listed, max_attempts=max_attempts,
            )
        if len(slots) > max_attempts:
            raise HistoryImportError("source file attempt budget exceeded")
        status = {
            "schema_version": "rival-gate-hourly-l2-resume-v1",
            "snapshot_at_utc": now.astimezone(UTC).isoformat(),
            "candidate_attempts": len(slots),
            "already_listed_not_byte_reverified": skipped,
            "readbacks": [], "blocked": [],
            "original_source_bytes_uploaded": 0,
            "source_months_qualified": 0, "full_hourly_coverage_verified": False,
            "future_only_model_approval": False,
        }
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            for asset, family, hour in slots:
                try:
                    data, evidence = await asyncio.to_thread(
                        download, client, asset=asset, family=family,
                        hour=hour, now=now,
                    )
                    if (status["original_source_bytes_uploaded"] + len(data)
                        > MAX_BATCH_SOURCE_BYTES):
                        status["blocked"].append({
                            "asset": asset, "family": family, "hour": hour,
                            "reason": "Gate L2 session original-byte budget reached",
                        })
                        break
                    result = await store(
                        drive, raw=data, asset=asset, family=family,
                        hour=hour, evidence=evidence, root=root, now=now,
                    )
                    status["original_source_bytes_uploaded"] += len(data)
                    status["readbacks"].append(result)
                except (HistoryImportError, httpx.HTTPError, OSError) as exc:
                    status["blocked"].append({
                        "asset": asset, "family": family, "hour": hour,
                        "reason": str(exc)[:180],
                    })
                await asyncio.sleep(0.15)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
        return status
    finally:
        await drive.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded, resumable original Gate L2 history")
    parser.add_argument("--hour", default=None, help="Manual UTC YYYYMMDDHH")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--root", type=Path, default=Path("data/gate-hourly-l2"))
    parser.add_argument(
        "--report", type=Path,
        default=Path("data/native-reports/gate-l2-batch.json"),
    )
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= MAX_ATTEMPTS:
        parser.error("max-attempts must be between 1 and 12")
    result = asyncio.run(collect_batch(
        now=datetime.now(UTC), root=args.root, report_path=args.report,
        max_attempts=args.max_attempts, selected_hour=args.hour,
    ))
    print(json.dumps({
        "source_originals_drive_readback": len(result["readbacks"]),
        "unavailable_or_rejected": len(result["blocked"]),
        "listed_only_not_reverified": result["already_listed_not_byte_reverified"],
        "full_historical_liquidity_verified": False,
    }, sort_keys=True))
    # A 404 or remote byte/size blocker is evidence, not a green history.
    # Do not fail other venues' independent jobs for a source-time 404.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
