"""Bounded, restart-safe collection of *research* archives, never live odds.

Run repeatedly as a separate low-priority scheduled process. The default
12-file budget avoids flooding source websites or exhausting host storage.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.research.free_history import (
    ASSETS, INTERVALS, LEAGUES, HistoryImportError,
    ingest_binance, ingest_football,
)


def complete_months(*, now: datetime, count: int) -> list[str]:
    if now.utcoffset() is None or not 1 <= count <= 36:
        raise ValueError("aware clock and 1..36 months required")
    clock = now.astimezone(UTC)
    months = []
    index = clock.year * 12 + clock.month - 2
    for offset in range(count - 1, -1, -1):
        year, month_zero = divmod(index - offset, 12)
        months.append(f"{year:04d}-{month_zero + 1:02d}")
    return months


def targets(
    *, months: list[str], assets: list[str], intervals: list[str],
    seasons: list[str], leagues: list[str],
) -> list[tuple[str, str, str, str]]:
    work = []
    for month in months:
        for asset in assets:
            for interval in intervals:
                work.append(("binance", asset, interval, month))
    for season in seasons:
        for league in leagues:
            work.append(("football", league, season, ""))
    return work


def _already_present(out: Path, job: tuple[str, str, str, str]) -> bool:
    if job[0] == "binance":
        source, key = "binance_um_klines", f"{job[1]}USDT-{job[2]}-{job[3]}"
    else:
        source, key = "football_data", f"{job[1]}-{job[2]}"
    for path in (out / source).glob(key + "--*.manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            file = manifest["file"]
            data = path.parent / file
            # Validate by digest, not merely filename presence.
            if (
                data.is_file() and data.name == file
                and manifest["source"] == source
                and __import__("hashlib").sha256(data.read_bytes()).hexdigest()
                == manifest["data_sha256"]
            ):
                return True
        except (OSError, KeyError, ValueError, TypeError):
            continue
    return False


def collect(
    client: httpx.Client, *, out: Path, jobs: list[tuple[str, str, str, str]],
    max_new: int = 12,
) -> dict:
    if not 1 <= max_new <= 100:
        raise ValueError("max-new must be 1..100")
    counts = {"planned": len(jobs), "already_verified": 0, "downloaded": 0,
              "rejected": 0, "remaining": 0}
    failed: list[dict[str, str]] = []
    for index, job in enumerate(jobs):
        if _already_present(out, job):
            counts["already_verified"] += 1
            continue
        if counts["downloaded"] + counts["rejected"] >= max_new:
            # Remaining jobs are revisited on a subsequent invocation.
            counts["remaining"] += 1
            continue
        try:
            if job[0] == "binance":
                ingest_binance(
                    client, asset=job[1], interval=job[2],
                    month=job[3], out_dir=out,
                )
            else:
                ingest_football(
                    client, league=job[1], season=job[2], out_dir=out,
                )
            counts["downloaded"] += 1
        except HistoryImportError as exc:
            counts["rejected"] += 1
            failed.append({
                "source": job[0], "key": "/".join(job[1:]),
                "reason": str(exc),
            })
    return {**counts, "failed": failed, "offline_research_only": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RivaL bounded free-history batch intake")
    parser.add_argument("--out", type=Path, default=Path("data/research-history"))
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS),
                        default=["BTC", "ETH", "SOL"])
    parser.add_argument("--intervals", nargs="+", choices=sorted(INTERVALS),
                        default=["5m", "15m", "1h", "4h", "1d"])
    parser.add_argument("--seasons", nargs="*", default=["2425", "2526"])
    parser.add_argument("--leagues", nargs="+", choices=sorted(LEAGUES),
                        default=["E0", "D1", "I1", "SP1", "F1"])
    parser.add_argument("--max-new", type=int, default=12)
    parser.add_argument("--plan", action="store_true",
                        help="Print intended sources without fetching or writing")
    args = parser.parse_args(argv)
    if len(set(args.assets)) != len(args.assets) or len(set(args.intervals)) != len(args.intervals):
        parser.error("duplicate asset or interval")
    if args.max_new < 1 or args.max_new > 100:
        parser.error("max-new must be 1..100")
    try:
        months = complete_months(now=datetime.now(UTC), count=args.months)
        if any(
            len(value) != 4 or not value.isascii() or not value.isdigit()
            or int(value[2:]) != (int(value[:2]) + 1) % 100
            for value in args.seasons
        ):
            raise ValueError("seasons must be four-digit codes such as 2526")
        jobs = targets(
            months=months, assets=args.assets, intervals=args.intervals,
            seasons=args.seasons, leagues=args.leagues,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.plan:
        print(json.dumps({"planned": len(jobs), "jobs": jobs}, indent=2))
        return 0
    with httpx.Client() as client:
        result = collect(client, out=args.out, jobs=jobs, max_new=args.max_new)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
