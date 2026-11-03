"""Official Binance USD-M auxiliary monthly ZIP backfill, original bytes first.

Never confuse a verified provider ZIP with a gap-free month or a live forecast.
All source URLs are constructed exclusively from hardcoded venue/family/asset enums.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.history_priority import EARLIEST_ARCHIVE_MONTH, ordered_months
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.run_budget import Pacer, RunBudget, drive_has_room

ASSETS = ("BTC", "ETH", "SOL", "XRP")
# Model inputs first (mark, index, premium, funding), then other intervals.
# Binance's dataset is "premiumIndexKlines"; the former "premiumPriceKlines"
# path does not exist, so premium was never collected before this fix.
FAMILIES = {
    "mark": ("markPriceKlines", "5m"),
    "index": ("indexPriceKlines", "5m"),
    "premium": ("premiumIndexKlines", "5m"),
    "funding": ("fundingRate", None),
    "candles_1m": ("klines", "1m"),
    "candles_15m": ("klines", "15m"),
    "candles_1h": ("klines", "1h"),
    "candles_4h": ("klines", "4h"),
    "candles_1d": ("klines", "1d"),
}
MAX_NEW_PER_RUN = 5000
MAX_ATTEMPTS_PER_RUN = 10000
DRIVE_ROOM_CHECK_EVERY = 25
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
               "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BASE = "https://data.binance.vision/data/futures/um/monthly"
MAX_ZIP = 24 * 1024 * 1024
MAX_CSV = 90 * 1024 * 1024
MANIFEST = re.compile(
    r"binance-aux-(BTC|ETH|SOL|XRP)-([a-z0-9_]+)-(20\d{2}-(?:0[1-9]|1[0-2]))"
    r"--[0-9a-f]{16}\.manifest\.json"
)


def source_url(asset: str, family: str, month: str) -> str:
    if asset not in ASSETS or family not in FAMILIES or not re.fullmatch(
        r"20\d{2}-(?:0[1-9]|1[0-2])", month,
    ) or month < EARLIEST_ARCHIVE_MONTH:
        raise ValueError("untrusted Binance auxiliary source identity")
    dataset, interval = FAMILIES[family]
    if family == "funding":
        name = f"{asset}USDT-fundingRate-{month}.zip"
        return f"{BASE}/{dataset}/{asset}USDT/{name}"
    name = f"{asset}USDT-{interval}-{month}.zip"
    return f"{BASE}/{dataset}/{asset}USDT/{interval}/{name}"


def inspect_original_zip(raw: bytes, *, asset: str, family: str,
                         month: str) -> dict:
    """Validate original month data without confusing funding events with bars."""
    url = source_url(asset, family, month)
    if not 0 < len(raw) <= MAX_ZIP:
        raise HistoryImportError("Binance auxiliary source outside ZIP size limit")
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    step = (None if family == "funding"
            else INTERVAL_MS[FAMILIES[family][1]])
    expected = None if step is None else (hi - lo) // step
    seen: set[int] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files = archive.infolist()
            expected_name = url.rsplit("/", 1)[-1].removesuffix(".zip") + ".csv"
            if (len(files) != 1 or files[0].filename != expected_name
                or files[0].file_size > MAX_CSV or files[0].is_dir()):
                raise HistoryImportError("Binance ZIP CSV identity or size invalid")
            with archive.open(files[0]) as source:
                reader = csv.reader(io.TextIOWrapper(source, encoding="utf-8-sig"))
                if family == "funding":
                    # Funding is irregular settlement EVENT data, never an
                    # OHLCV or guaranteed 8-hour fixed interval candle grid.
                    header = next(reader, [])
                    if [field.strip() for field in header] != [
                        "calc_time", "funding_interval_hours", "last_funding_rate",
                    ]:
                        raise HistoryImportError("Binance funding CSV header incompatible")
                    for fields in reader:
                        if not fields:
                            continue
                        if (len(fields) != 3 or not fields[0].isascii()
                            or not fields[0].isdigit()):
                            raise HistoryImportError("Binance funding event malformed")
                        tick = int(fields[0])
                        if tick < lo or tick >= hi or tick in seen:
                            raise HistoryImportError("Binance funding event off-month/duplicate")
                        try:
                            interval = Decimal(fields[1])
                            rate = Decimal(fields[2])
                        except InvalidOperation as exc:
                            raise HistoryImportError("Binance funding rate invalid") from exc
                        if (not interval.is_finite() or not rate.is_finite()
                            or not 0 < interval <= 24 or abs(rate) > 1):
                            raise HistoryImportError("Binance funding event invalid range")
                        seen.add(tick)
                else:
                    for fields in reader:
                        if fields and fields[0].lower() in ("open_time", "open time"):
                            continue
                        if (len(fields) < 5 or not fields[0].isascii()
                            or not fields[0].isdigit()):
                            raise HistoryImportError("Binance auxiliary CSV row malformed")
                        tick = int(fields[0])
                        if (tick < lo or tick >= hi or (tick - lo) % step != 0
                            or tick in seen or len(seen) >= expected):
                            raise HistoryImportError("Binance auxiliary duplicate/off-grid row")
                        try:
                            op, high, low, close = [Decimal(v) for v in fields[1:5]]
                        except InvalidOperation as exc:
                            raise HistoryImportError("Binance auxiliary OHLC invalid") from exc
                        if (not all(v.is_finite() for v in (op, high, low, close))
                            or high < max(op, close) or low > min(op, close)
                            or high < low or (family != "premium" and low <= 0)):
                            raise HistoryImportError("Binance auxiliary OHLC invalid")
                        seen.add(tick)
    except (zipfile.BadZipFile, UnicodeError, OSError, csv.Error) as exc:
        raise HistoryImportError("Binance auxiliary ZIP or CSV invalid") from exc
    if family == "funding":
        if not seen:
            raise HistoryImportError("Binance funding archive contains no source events")
        return {
            "rows": len(seen), "expected_rows": None,
            "complete_utc_grid": False, "missing_slots": None,
            "interval_minutes": None, "source_event_timestamps_validated": True,
            "complete_funding_settlement_history_proven": False,
        }
    return {
        "rows": len(seen), "expected_rows": expected,
        "complete_utc_grid": len(seen) == expected,
        "missing_slots": expected - len(seen),
        "interval_minutes": step // 60_000,
    }

async def fetch_original(client: httpx.AsyncClient, url: str, *, cap: int) -> bytes:
    if not url.startswith(BASE + "/") or "?" in url or "#" in url:
        raise ValueError("untrusted Binance original source URL")
    for attempt in range(3):
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code == 404:
                raise HistoryImportError("Binance archive HTTP 404 not proven permanently absent")
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == 2:
                    raise HistoryImportError(f"Binance archive HTTP {response.status_code} exhausted")
            elif response.status_code != 200:
                raise HistoryImportError(f"Binance archive HTTP {response.status_code}")
            else:
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > cap:
                        raise HistoryImportError("Binance source response exceeds safe size")
                return bytes(buf)
        await asyncio.sleep(2 ** (attempt + 1))
    raise AssertionError("bounded Binance archive transport exhausted")


def checksum(raw: bytes, checksum_raw: bytes, *, file_name: str) -> str:
    if len(checksum_raw) > 512:
        raise HistoryImportError("oversized Binance provider checksum")
    try:
        rows = checksum_raw.decode("ascii").strip().split()
    except UnicodeError as exc:
        raise HistoryImportError("non-ASCII Binance checksum") from exc
    if (len(rows) != 2 or rows[1].removeprefix("*") != file_name
        or not re.fullmatch("[0-9a-fA-F]{64}", rows[0])):
        raise HistoryImportError("Binance provider checksum format invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if rows[0].lower() != digest:
        raise HistoryImportError("Binance provider original ZIP SHA256 mismatch")
    return digest


async def existing(drive) -> set[tuple[str, str, str]]:
    await drive._verify_folder_boundary()
    folder = drive.folders.manifests
    q = (f"'{drive._escape_query(folder)}' in parents and trashed = false "
         "and name contains 'binance-aux-' "
         "and appProperties has { key='rival_kind' and value='manifest' }")
    known = set()
    page = None
    for _ in range(12):
        params = {"q": q, "spaces": "drive", "pageSize": 1000,
                  "fields": "nextPageToken,files(name,parents,trashed)"}
        if page:
            params["pageToken"] = page
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params, headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("Binance source manifest inventory unavailable")
        doc = response.json()
        if not isinstance(doc, dict) or not isinstance(doc.get("files"), list):
            raise HistoryImportError("malformed Binance source inventory")
        for f in doc["files"]:
            if f.get("trashed") or folder not in f.get("parents", []):
                raise HistoryImportError("Binance source manifest outside Drive boundary")
            match = MANIFEST.fullmatch(f.get("name", ""))
            if match and match.group(2) in FAMILIES:
                known.add(match.groups())
        page = doc.get("nextPageToken")
        if page is None:
            return known
        if not isinstance(page, str) or not page:
            raise HistoryImportError("Binance source inventory cursor invalid")
    raise HistoryImportError("Binance source inventory exceeded bounded pages")


async def collect(*, drive, client: httpx.AsyncClient, out_dir: Path,
                  now: datetime | None = None, max_new: int = 24,
                  max_attempts: int = 48, budget: RunBudget | None = None,
                  pacer: Pacer | None = None) -> dict:
    if (not 1 <= max_new <= MAX_NEW_PER_RUN
            or not 1 <= max_attempts <= MAX_ATTEMPTS_PER_RUN):
        raise ValueError("bounded Binance auxiliary archive limits required")
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    y, m = clock.year, clock.month
    end = y * 12 + m - 1
    first_year, first_month = map(int, EARLIEST_ARCHIVE_MONTH.split("-"))
    months = [f"{yr:04d}-{mo + 1:02d}" for yr, mo in
              (divmod(n, 12) for n in range(first_year * 12 + first_month - 1, end))]
    listed = await existing(drive)
    # Recent months first (ordered_months), model-input families first within
    # a month. Pre-listing months return 404 and are reported, never filled.
    tasks = [(asset, fam, month) for month in ordered_months(months, now=clock)
             for fam in FAMILIES for asset in ASSETS
             if (asset, fam, month) not in listed]
    pacer = pacer or Pacer()
    report = {"schema_version": "rival-binance-original-aux-v1",
              "created_at": clock.isoformat(), "attempted": 0, "verified": [],
              "unavailable_unconfirmed": [], "model_live_approved": False,
              "stopped_reason": "backlog_empty"}
    out_dir.mkdir(parents=True, exist_ok=True)
    for asset, fam, month in tasks:
        if report["attempted"] >= max_attempts or len(report["verified"]) >= max_new:
            report["stopped_reason"] = "run_limit_reached"
            break
        if budget is not None and budget.expired():
            report["stopped_reason"] = "time_budget_reached"
            break
        if (report["verified"]
                and len(report["verified"]) % DRIVE_ROOM_CHECK_EVERY == 0
                and not await drive_has_room(drive)):
            report["stopped_reason"] = "drive_storage_guard"
            break
        await pacer.wait()
        report["attempted"] += 1
        url = source_url(asset, fam, month)
        try:
            zipped = await fetch_original(client, url, cap=MAX_ZIP)
            signed_checksum = await fetch_original(client, url + ".CHECKSUM", cap=512)
            original_digest = checksum(
                zipped, signed_checksum, file_name=url.rsplit("/", 1)[-1],
            )
            quality = inspect_original_zip(zipped, asset=asset, family=fam, month=month)
            prefix = f"binance-aux-{asset}-{fam}-{month}--{original_digest[:16]}"
            original = out_dir / (prefix + ".zip")
            original.write_bytes(zipped)
            checksum_file = out_dir / (prefix + ".zip.CHECKSUM")
            checksum_file.write_bytes(signed_checksum)
            proof = out_dir / (prefix + ".manifest.json")
            proof.write_text(json.dumps({
                "venue": "binance", "asset": asset, "family": fam,
                "source_url": url, "source_sha256": original_digest,
                "provider_signed_checksum": False,
                "provider_published_checksum_sha256_verified": True,
                "original_zip_preserved": True,
                **quality, "live_model_approved": False,
                "retrospective_source_only": True,
            }, sort_keys=True))
            artifact = _artifact(original, quality["rows"])
            stored = await drive.ensure_history_file(
                artifact=artifact, batch_id="binance-aux-" + original_digest[:24],
                mime_type="application/zip",
            )
            await drive.verify_downloaded_artifact(
                file_id=stored.file_id, parent_id=drive.folders.history,
                artifact=artifact,
            )
            checksum_artifact = _artifact(checksum_file, 1)
            checksum_saved = await drive.ensure_history_file(
                artifact=checksum_artifact,
                batch_id="binance-aux-checksum-" + checksum_artifact.sha256[:24],
                mime_type="text/plain",
            )
            await drive.verify_downloaded_artifact(
                file_id=checksum_saved.file_id, parent_id=drive.folders.history,
                artifact=checksum_artifact,
            )
            manifest_artifact = _artifact(proof, 1)
            saved = await drive.ensure_manifest_file(
                artifact=manifest_artifact, batch_id="binance-aux-m-" +
                manifest_artifact.sha256[:24],
            )
            await drive.verify_downloaded_artifact(
                file_id=saved.file_id, parent_id=drive.folders.manifests,
                artifact=manifest_artifact,
            )
            report["verified"].append({
                "asset": asset, "family": fam, "month": month,
                "original_provider_zip_drive_sha_verified": True, **quality,
            })
        except (HistoryImportError, httpx.HTTPError, OSError, ValueError,
                RuntimeError) as exc:
            # RuntimeError covers DriveArchiveError: one failed upload is
            # recorded and retried next run, never the end of the whole run.
            report["unavailable_unconfirmed"].append({
                "asset": asset, "family": fam, "month": month,
                "reason": str(exc)[:160],
            })
    report["backlog_remaining"] = max(0, len(tasks) - report["attempted"])
    report_path = out_dir / "binance-auxiliary-progress.json"
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    artifact = _artifact(report_path, 1)
    await drive.ensure_research_output(
        artifact=artifact, batch_id="binance-aux-report-" + artifact.sha256[:24],
        kind="report",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-new", type=int, default=24)
    parser.add_argument("--max-attempts", type=int, default=48)
    parser.add_argument("--time-budget-seconds", type=int, default=0,
                        help="stop after this many seconds (0 = no time limit)")
    args = parser.parse_args()
    budget = RunBudget(args.time_budget_seconds) if args.time_budget_seconds else None
    async def worker() -> dict:
        drive = _drive(Settings())
        try:
            if not await drive_has_room(drive):
                raise HistoryImportError("Drive storage guard: account above 90% full")
            async with httpx.AsyncClient(timeout=50, follow_redirects=False) as client:
                return await collect(
                    drive=drive, client=client, out_dir=Path("data/binance-aux"),
                    max_new=args.max_new, max_attempts=args.max_attempts,
                    budget=budget,
                )
        finally:
            await drive.aclose()
    result = asyncio.run(worker())
    print(json.dumps({
        "attempted": result["attempted"], "verified": len(result["verified"]),
        "unavailable_unconfirmed": len(result["unavailable_unconfirmed"]),
        "backlog_remaining": result["backlog_remaining"],
        "stopped_reason": result["stopped_reason"],
        "history_complete": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
