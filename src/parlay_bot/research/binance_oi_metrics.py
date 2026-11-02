"""Binance Vision original DAILY 5m futures open-interest/positioning metrics.

A daily ZIP contains sum_open_interest and independent long/short positioning
ratios. Keep the exact exchange ZIP/CHECKSUM; never backfill synthetic slots or
treat a partially populated day as a complete month. Historical data only.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.drive_source_census import list_names
from parlay_bot.research.free_history import (
    HistoryImportError,
    _checked_zip,
    _fetch,
)
from parlay_bot.research.publish import _artifact, _drive

ASSETS = ("BTC", "ETH", "SOL", "XRP")
SOURCE = "https://data.binance.vision/data/futures/um/daily/metrics"
NAME = re.compile(
    r"binance-metrics-(BTC|ETH|SOL|XRP)-(20\d{2}-\d{2}-\d{2})"
    r"--[0-9a-f]{16}\.manifest\.json"
)
REQUIRED = frozenset({
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
})
MAX_NEW = 32
STEP = timedelta(minutes=5)


def url(asset: str, day: date, *, now: datetime) -> str:
    if asset not in ASSETS or now.utcoffset() is None:
        raise HistoryImportError("invalid Binance metrics scope")
    if not date(2021, 1, 1) <= day <= now.astimezone(UTC).date() - timedelta(days=2):
        raise HistoryImportError("metrics day outside completed/archive grace range")
    symbol = asset + "USDT"
    return f"{SOURCE}/{symbol}/{symbol}-metrics-{day.isoformat()}.zip"


def _number(raw: str, *, required: bool) -> str | None:
    if raw is None or not raw.strip():
        if required:
            raise HistoryImportError("missing required open interest metric")
        return None
    try:
        num = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise HistoryImportError("non-numeric Binance metrics") from exc
    if not num.is_finite() or num < 0:
        raise HistoryImportError("negative or infinite Binance metric")
    return str(num)


def validate_csv(data: bytes, *, asset: str, day: date) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise HistoryImportError("metrics CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if (reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames))
        or len(reader.fieldnames) > 24 or not REQUIRED <= set(reader.fieldnames)):
        raise HistoryImportError("Binance open interest metrics columns changed")
    first = datetime.combine(day, datetime.min.time(), UTC)
    observed = set()
    missing_optional = 0
    first_observed = None
    last_observed = None
    for record in reader:
        if len(observed) >= 288 or None in record:
            raise HistoryImportError("unbounded Binance daily metrics")
        try:
            when = datetime.strptime(
                record["create_time"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise HistoryImportError("metrics timestamp wrong type") from exc
        if (not first <= when < first + timedelta(days=1)
            or (when - first).total_seconds() % 300
            or when in observed or record["symbol"] != asset + "USDT"):
            raise HistoryImportError("wrong symbol, duplicate or non-5m metrics row")
        observed.add(when)
        first_observed = min(first_observed, when) if first_observed else when
        last_observed = max(last_observed, when) if last_observed else when
        for key in REQUIRED - {"symbol", "create_time"}:
            missing_optional += (
                _number(record[key], required=key in {
                    "sum_open_interest", "sum_open_interest_value",
                }) is None
            )
    if not observed:
        raise HistoryImportError("Binance metrics source day empty")
    exact_grid = (
        len(observed) == 288
        and first_observed == first
        and last_observed == first + timedelta(days=1) - STEP
    )
    return {
        "source_records": len(observed),
        "source_day_complete_exact_5m_grid": exact_grid,
        "missing_optional_metric_values": missing_optional,
        "first_observed_utc": first_observed.isoformat(),
        "last_observed_utc": last_observed.isoformat(),
        "source_calendar_day": day.isoformat(),
        "source_family": ["open_interest", "positioning"],
        "full_calendar_month_verified": False,
        "source_data_research_only": True,
    }


def download(client: httpx.Client, *, asset: str,
             day: date, now: datetime) -> tuple[bytes, bytes, dict]:
    target = url(asset, day, now=now)
    name = target.rsplit("/", 1)[-1]
    checksum = _fetch(client, target + ".CHECKSUM", limit=1024)
    try:
        line = checksum.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise HistoryImportError("metrics CHECKSUM encoding invalid") from exc
    matched = re.fullmatch(
        rf"([a-fA-F0-9]{{64}})\s+\*?{re.escape(name)}", line,
    )
    if not matched:
        raise HistoryImportError("metrics checksum is not provider SHA256")
    raw = _fetch(client, target, limit=32 * 1024 * 1024)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != matched.group(1).lower():
        raise HistoryImportError("Binance original metrics ZIP checksum mismatch")
    csv_bytes = _checked_zip(raw, name.removesuffix(".zip") + ".csv")
    verified = validate_csv(csv_bytes, asset=asset, day=day)
    return raw, checksum, {
        **verified, "zip_sha256": digest,
        "checksum_sha256": hashlib.sha256(checksum).hexdigest(),
        "provider_signed_digest": False,
        "provider_published_checksum": True,
        "original_source_url": target,
        "original_zip_retained": True,
        "causal_available_at_verified": False,
        "live_model_admission": False,
    }


def candidate_days(*, now: datetime, available: set[tuple[str, str]],
                   max_new: int = MAX_NEW) -> list[tuple[str, date]]:
    if now.utcoffset() is None or not 1 <= max_new <= MAX_NEW:
        raise ValueError("invalid metrics batch size or clock")
    cutoff = now.astimezone(UTC).date() - timedelta(days=2)
    begin = date(2021, 1, 1)
    age = (cutoff - begin).days
    if age < 0:
        return []
    # Mix recent and old days, so the 2021 archive never blocks last week's
    # newly released data. Rotate older dates across unattended runs to avoid
    # retrying a permanently unavailable pre-listing day every six hours.
    recent = [cutoff - timedelta(days=delta) for delta in range(20)]
    recent = [d for d in recent if d >= begin]
    older = []
    shift = now.date().toordinal() % (age + 1)
    for offset in range(age + 1):
        day = begin + timedelta(days=(shift + offset) % (age + 1))
        if day not in recent:
            older.append(day)
    slots = []
    for group in (recent, older):
        for day in group:
            for asset in ASSETS:
                if (asset, day.isoformat()) not in available:
                    slots.append((asset, day))
                    if len(slots) == max_new:
                        return slots
    return slots


def existing_daily(names: list[str]) -> set[tuple[str, str]]:
    found = set()
    for name in names:
        match = NAME.fullmatch(name)
        if match:
            found.add((match.group(1), match.group(2)))
    return found


def _save(path: Path, data: bytes) -> None:
    if path.exists() and path.read_bytes() != data:
        raise HistoryImportError("historical source artifact conflict")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


async def publish_day(drive, *, root: Path, asset: str, day: date,
                      raw: bytes, checksum: bytes, meta: dict) -> dict:
    stem = f"binance-metrics-{asset}-{day.isoformat()}--{meta['zip_sha256'][:16]}"
    original = root / (stem + ".zip")
    proof = root / (stem + ".CHECKSUM")
    manifest = root / (stem + ".manifest.json")
    document = {
        "schema_version": "rival-binance-metrics-day-original-v1",
        "venue": "binance", "asset": asset, "family": ["open_interest", "positioning"],
        "day_utc": day.isoformat(), "metrics": meta,
        "original_zip_filename": original.name, "checksum_filename": proof.name,
        "whole_calendar_month_verified": False, "model_training_approved": False,
    }
    _save(original, raw)
    _save(proof, checksum)
    _save(manifest, (json.dumps(document, sort_keys=True, indent=2) + "\n").encode())
    batch = "binance-metrics-" + meta["zip_sha256"][:24]
    for path in (original, proof):
        item = _artifact(path, 1)
        target = await drive.ensure_history_file(
            artifact=item, batch_id=batch + ("-check" if path == proof else ""),
            mime_type=("text/plain" if path == proof else "application/zip"),
        )
        await drive.verify_downloaded_artifact(
            file_id=target.file_id, parent_id=drive.folders.history,
            artifact=item, max_bytes=32 * 1024 * 1024,
        )
    item = _artifact(manifest, 1)
    receipt = await drive.ensure_manifest_file(
        artifact=item, batch_id=batch,
    )
    await drive.verify_downloaded_artifact(
        file_id=receipt.file_id, parent_id=drive.folders.manifests, artifact=item,
    )
    return {
        "asset": asset, "day": day.isoformat(),
        "source_records": meta["source_records"],
        "whole_day_5m_verified": meta["source_day_complete_exact_5m_grid"],
        "original_zip_and_checksum_drive_sha256_readback": True,
        "full_month_verified": False,
    }


async def collect(*, now: datetime, root: Path, report_dir: Path,
                  max_new: int = MAX_NEW) -> dict:
    if now.utcoffset() is None or not 1 <= max_new <= MAX_NEW:
        raise ValueError("invalid metrics acquisition inputs")
    drive = _drive(Settings())
    try:
        available = existing_daily(await list_names(drive))
        slots = candidate_days(now=now, available=available, max_new=max_new)
        saved = []
        problems = []
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            for asset, day in slots:
                try:
                    raw, checksum, proof = await asyncio.to_thread(
                        download, client, asset=asset, day=day, now=now,
                    )
                    saved.append(await publish_day(
                        drive, root=root, asset=asset, day=day,
                        raw=raw, checksum=checksum, meta=proof,
                    ))
                except (HistoryImportError, httpx.HTTPError, OSError) as exc:
                    problems.append({
                        "asset": asset, "day": day.isoformat(),
                        "status": "source_failed_or_unavailable_not_proven_prelisting",
                        "reason": str(exc)[:180],
                    })
                await asyncio.sleep(0.2)
        report = {
            "schema_version": "rival-binance-daily-oi-original-v1",
            "generated_at": now.astimezone(UTC).isoformat(),
            "already_listed_not_reverified": len(available),
            "attempted": len(slots), "readback_verified_days": saved,
            "unavailable_or_rejected": problems,
            "calendar_months_qualified": 0, "research_only": True,
            "model_promoted": False,
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "binance-metrics-original-progress.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report
    finally:
        await drive.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-new", type=int, default=16)
    parser.add_argument("--root", type=Path, default=Path("data/binance-metrics-original"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args()
    result = asyncio.run(collect(
        now=datetime.now(UTC), root=args.root,
        report_dir=args.report_dir, max_new=args.max_new,
    ))
    print(json.dumps({
        "attempted": result["attempted"],
        "original_days_sha_readback": len(result["readback_verified_days"]),
        "missing_or_failed": len(result["unavailable_or_rejected"]),
        "full_calendar_months_proven": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
