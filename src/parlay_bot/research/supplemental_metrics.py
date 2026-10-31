"""Source-provenance-safe daily Binance futures positioning context.

Binance UM metrics changed create_time labeling around June 25, 2026.
Keep archived source times, label regimes and a conservative usable-after
timestamp separate; never turn these rows into live observations or labels.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.research.free_history import ASSETS, HistoryImportError, _persist, _sha
from parlay_bot.research.supplemental_history import _binance_download, _resume_verified

BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
SOURCE = "binance_um_daily_metrics"
FIELDS = (
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)
# Source issue #491 reports a label convention flip, not an authoritative
# guarantee of every publication timestamp or all fields' capture semantics.
REGIME_FLIP = date(2026, 6, 25)
_STEP = timedelta(minutes=5)


def daily_metrics_url(*, asset: str, day: date) -> str:
    if asset not in ASSETS or type(day) is not date:
        raise HistoryImportError("unsupported metrics asset or date")
    today = datetime.now(UTC).date()
    if day >= today or day < date(2020, 1, 1):
        raise HistoryImportError("only complete past days qualify for metrics research")
    value = day.isoformat()
    symbol = asset + "USDT"
    return f"{BASE}/{symbol}/{symbol}-metrics-{value}.zip"


def _number(value: str) -> float:
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HistoryImportError("invalid historical positioning measurement") from exc
    if not math.isfinite(v) or v < 0 or v > 1e16:
        raise HistoryImportError("invalid or negative positioning measurement")
    return v


def parse_daily_metrics(payload: bytes, *, asset: str, day: date) -> list[dict]:
    if asset not in ASSETS:
        raise HistoryImportError("unsupported historical metrics asset")
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        names = set(reader.fieldnames or ())
    except UnicodeError as exc:
        raise HistoryImportError("invalid historical metrics encoding") from exc
    if not {"create_time", "symbol", *FIELDS} <= names:
        raise HistoryImportError("historical metrics schema missing fields")
    result = []
    previous = None
    for row in reader:
        if len(result) >= 300:
            raise HistoryImportError("excessive 5-minute positioning rows")
        if row.get("symbol") != asset + "USDT":
            raise HistoryImportError("wrong-market metrics archive")
        timestamp = row.get("create_time")
        if not isinstance(timestamp, str):
            raise HistoryImportError("missing metrics time")
        try:
            labeled = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HistoryImportError("invalid metrics timestamp") from exc
        if labeled.utcoffset() is None:
            labeled = labeled.replace(tzinfo=UTC)
        labeled = labeled.astimezone(UTC)
        if (labeled.second or labeled.microsecond or labeled.minute % 5
            or (previous is not None and labeled <= previous)
            or labeled.date() not in (day, day + timedelta(days=1))
            or (labeled.date() != day and labeled.time() != datetime.min.time())):
            raise HistoryImportError("untrustworthy metrics chronology or source day")
        # Old source files were end-labeled, new files start-labeled; the
        # transition has two colliding labels across adjacent daily archives.
        # A full source publication delay is NOT known. These are strict
        # offline earliest-eligible *feature timestamps*, not observed_at.
        regime = "end_labeled" if day < REGIME_FLIP else "start_labeled"
        usable_after = labeled + (_STEP if regime == "end_labeled" else 2 * _STEP)
        values = {key: _number(row[key]) for key in FIELDS}
        result.append({
            "source_date": day.isoformat(),
            "source_labeled_at": labeled.isoformat(),
            "conservative_feature_after_at": usable_after.isoformat(),
            "label_regime": regime,
            "boundary_caution": day in (date(2026, 6, 24), date(2026, 6, 25)),
            **values,
        })
        previous = labeled
    if not result:
        raise HistoryImportError("empty or missing Binance metrics observations")
    # Before the label change one day's last source timestamp was the next
    # 00:00; after it, files span 00:00..23:55. Never concatenate by label
    # without source_date/regime (the transition contains collisions).
    if result[0]["source_labeled_at"][:10] not in (day.isoformat(),
                                                    (day + timedelta(days=1)).isoformat()):
        raise HistoryImportError("invalid metrics daily boundary")
    return result


def ingest_daily_metrics(
    client: httpx.Client, *, asset: str, day: date, out_dir: Path,
):
    url = daily_metrics_url(asset=asset, day=day)
    data, source_sha = _binance_download(client, url)
    rows = parse_daily_metrics(data, asset=asset, day=day)
    return _persist(
        source=SOURCE, key=f"{asset}USDT-metrics-{day.isoformat()}",
        records=rows, raw_sha=source_sha, url=url, out_dir=out_dir,
        extra={
            "venue": "binance", "contract": asset + "USDT",
            "quote": "USDT", "market_type": "usd_m_linear_futures",
            "feature_family": "open_interest_and_positioning",
            "source_day": day.isoformat(),
            "checksum_verified": True,
            "label_regime": "end_labeled" if day < REGIME_FLIP else "start_labeled",
            "timestamp_convention_change": "2026-06-25",
            "source_issue": "https://github.com/binance/binance-public-data/issues/491",
            "timestamp_caution": True,
            "historical_observation_not_prospective_first_seen_at": True,
            "no_verified_executable_orderbook_or_actual_fills": True,
            "not_an_exact_polymarket_or_kalshi_oracle": True,
        },
    )


def _days(count: int, *, now: datetime) -> list[date]:
    if not 1 <= count <= 220 or now.utcoffset() is None:
        raise ValueError("metrics catch-up requires 1..220 past days and aware clock")
    today = now.astimezone(UTC).date()
    return [today - timedelta(days=n) for n in range(count, 0, -1)]


def collect_daily_metrics(
    client: httpx.Client, *, root: Path, days: list[date],
    assets: list[str], max_new: int = 80,
) -> dict:
    if not 1 <= max_new <= 100 or len(set(days)) != len(days):
        raise ValueError("invalid metrics workload")
    if not assets or len(set(assets)) != len(assets) or not set(assets) <= ASSETS:
        raise ValueError("invalid metrics asset set")
    jobs = [(asset, day) for day in days for asset in assets]
    summary = {"planned": len(jobs), "already_verified": 0, "downloaded": 0,
               "rejected": 0, "remaining": 0, "failed": [], "unverified_rows_used_live": 0}
    for asset, day in jobs:
        key = f"{asset}USDT-metrics-{day.isoformat()}"
        if _resume_verified(root, SOURCE, key):
            summary["already_verified"] += 1
            continue
        if summary["downloaded"] + summary["rejected"] >= max_new:
            summary["remaining"] += 1
            continue
        try:
            ingest_daily_metrics(client, asset=asset, day=day, out_dir=root)
            summary["downloaded"] += 1
        except HistoryImportError as exc:
            summary["rejected"] += 1
            summary["failed"].append({
                "asset": asset, "date": day.isoformat(), "reason": str(exc),
            })
    return summary


def _metrics_report(*, root: Path, days: list[date], assets: list[str], status: dict) -> dict:
    saved = sum(
        _resume_verified(root, SOURCE, f"{asset}USDT-metrics-{day.isoformat()}")
        for day in days for asset in assets
    )
    return {
        "schema": "rival-daily-metrics-history-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "requested_days": len(days), "requested_assets": assets,
        "verified": saved, "requested": len(days) * len(assets),
        "missing": len(days) * len(assets) - saved,
        "acquisition": status, "time_regime_flip_day": REGIME_FLIP.isoformat(),
        "no_historical_publication_clock_proof": True,
        "research_only": True, "prospective_forecast": False,
        "live_model_promoted": False, "actual_trading_enabled": False,
    }


async def _publish_metrics(root: Path, report_path: Path) -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.research.publish import _artifact, _drive, prepare, publish

    drive = _drive(Settings())
    try:
        # Durable checked-upload journal is restored with the validated cache.
        # Record each entry ONLY after the existing publisher verifies the
        # actual Drive download, otherwise the retry remains pending.
        folder = root / SOURCE
        journal = folder / "_published_restore_journal.json"
        try:
            old_entries = json.loads(journal.read_text(encoding="utf-8"))
            if not isinstance(old_entries, dict):
                old_entries = {}
        except (OSError, ValueError):
            old_entries = {}
        completed = 0
        for path in sorted(folder.glob("*.manifest.json")):
            digest = _sha(path.read_bytes())
            # Journal entries are just retry optimization, NOT current-time
            # proof of Drive access or a substitute for a checksum validation.
            if isinstance(old_entries.get(digest), dict):
                continue
            receipt = await publish(drive, prepare(path))
            old_entries[digest] = {
                "batch_id": receipt["batch_id"],
                "history_id": receipt["history_id"],
                "manifest_id": receipt["manifest_id"],
                "verified_at": datetime.now(UTC).isoformat(),
            }
            temp = journal.with_suffix(".tmp")
            temp.write_text(json.dumps(old_entries, sort_keys=True) + "\n", encoding="utf-8")
            temp.replace(journal)
            completed += 1
        art = _artifact(report_path, 1)
        result = await drive.ensure_research_output(
            artifact=art, batch_id="metrics-" + art.sha256[:24], kind="report",
        )
        return {"new_partitions_restored": completed, "previously_verified_partitions": len(old_entries) - completed,
                "report_id": result.file_id, "new_download_bytes_verified": True,
                "historical_journal_not_live_drive_recheck": True}
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conservative Binance positioning research")
    parser.add_argument("--root", type=Path, default=Path("data/research-history"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/research-reports"))
    parser.add_argument("--days", type=int, default=215)
    parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS), default=["BTC", "ETH", "SOL"])
    parser.add_argument("--max-new", type=int, default=80)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        days = _days(args.days, now=datetime.now(UTC))
        if args.plan:
            print(json.dumps({"source": SOURCE, "days": len(days),
                              "planned": len(days) * len(args.assets),
                              "max_new": args.max_new}, sort_keys=True))
            return 0
        with httpx.Client() as client:
            acquired = collect_daily_metrics(client, root=args.root, days=days,
                                              assets=args.assets, max_new=args.max_new)
        report = _metrics_report(root=args.root, days=days, assets=args.assets,
                                 status=acquired)
        args.report_dir.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
        path = args.report_dir / f"metrics-{_sha(raw)[:20]}.json"
        if path.exists() and path.read_bytes() != raw:
            raise HistoryImportError("conflicting daily metrics report")
        path.write_bytes(raw)
        if args.publish:
            report["drive"] = asyncio.run(_publish_metrics(args.root, path))
        print(json.dumps({"acquisition": acquired, "verified": report["verified"],
                          "missing": report["missing"], "report": str(path),
                          "drive": report.get("drive")}, sort_keys=True))
        return 1 if acquired["rejected"] else 0
    except (HistoryImportError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Daily positioning history blocked: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
