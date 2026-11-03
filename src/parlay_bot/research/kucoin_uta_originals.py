"""KuCoin UTA v2 original historical candles, index/mark/premium and open interest.

Files are private Drive originals, never GitHub artifacts or model imports.
Actual endpoint retention and market listing must be independently verified.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

BASE = "https://api.kucoin.com/api/ua/v2/market"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
CONTRACTS = {"BTC": "XBTUSDTM", "ETH": "ETHUSDTM",
             "SOL": "SOLUSDTM", "XRP": "XRPUSDTM"}
# Trade 5m already has an independent Discloud collector; do not duplicate it.
KLINES = {
    "candles_1m": ("TRADE", "1min", 60),
    "candles_15m": ("TRADE", "15min", 900),
    "candles_1h": ("TRADE", "1hour", 3600),
    "candles_4h": ("TRADE", "4hour", 14400),
    "candles_1d": ("TRADE", "1day", 86400),
    "mark": ("MARK_PRICE", "1hour", 3600),
    "index": ("INDEX_PRICE", "1hour", 3600),
    "premium": ("PREMIUM_INDEX", "1hour", 3600),
}
FAMILIES = tuple(KLINES) + ("open_interest",)
MAX_RAW_BYTES = 2_000_000
MAX_CANDLES = 200
MAX_OI_ROWS = 100
# Only current short-retention OI is obtainable. This is NOT old archive access.
OI_RETENTION_DAYS = {"1hour": 7, "1day": 70}
MIN_FINE = "2025-01"
MIN_COARSE_ATTEMPT = "2021-01"


def _clock(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def bounds(month: str, family: str, *, now: datetime | None = None):
    if family not in KLINES or len(month) != 7 or month[4] != "-":
        raise HistoryImportError("invalid KuCoin history scope")
    try:
        year, month_number = (int(x) for x in month.split("-"))
        start = datetime(year, month_number, 1, tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid UTC source month") from exc
    today = _clock(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    minimum = MIN_COARSE_ATTEMPT if KLINES[family][2] >= 28800 else MIN_FINE
    if month < minimum or start >= today:
        raise HistoryImportError("month outside provider's public attempt range")
    return start, (start + timedelta(days=32)).replace(day=1)


def select_month(family: str, *, now: datetime | None = None) -> str:
    if family not in KLINES:
        raise HistoryImportError("unknown original history family")
    clock = _clock(now)
    minimum = MIN_COARSE_ATTEMPT if KLINES[family][2] >= 28800 else MIN_FINE
    year, month = (int(x) for x in minimum.split("-"))
    options = []
    while (year, month) < (clock.year, clock.month):
        options.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if not options:
        raise HistoryImportError("no completed KuCoin source month")
    # Stable rotation, newest source months prioritized but older not starved.
    priority = list(reversed(options[-12:])) + options[:-12]
    return priority[(int(clock.timestamp()) // (6 * 3600) // len(KLINES)) % len(priority)]


def choose_family(now: datetime | None = None) -> str:
    return tuple(KLINES)[(int(_clock(now).timestamp()) // (6 * 3600)) % len(KLINES)]


def split_month(start: datetime, end: datetime, period_seconds: int):
    """180-candle windows avoid the provider's 200-row truncation."""
    seconds = 180 * period_seconds
    cursor = start
    while cursor < end:
        right = min(cursor + timedelta(seconds=seconds), end)
        yield cursor, right
        cursor = right


def kline_params(family: str, asset: str, start: datetime, end: datetime):
    if family not in KLINES or asset not in ASSETS or not start < end:
        raise HistoryImportError("bad source family/asset/window")
    kind, interval, _ = KLINES[family]
    return {
        "tradeType": "FUTURES", "symbol": CONTRACTS[asset],
        "klineType": kind, "interval": interval,
        "startAt": int(start.timestamp()), "endAt": int(end.timestamp()) - 1,
    }


def finite_number(value: object, *, nonnegative: bool = False,
                  positive: bool = False) -> float:
    if type(value) is bool:
        raise HistoryImportError("invalid boolean market value")
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryImportError("non-numeric original market value") from exc
    if not math.isfinite(num) or (positive and num <= 0) or (
        nonnegative and num < 0
    ):
        raise HistoryImportError("invalid original market value")
    return num


def validate_klines(raw: bytes, *, family: str, asset: str,
                    start: datetime, end: datetime) -> list[int]:
    if family not in KLINES or asset not in ASSETS or not 0 < len(raw) <= MAX_RAW_BYTES:
        raise HistoryImportError("untrusted KuCoin candle source")
    try:
        value = json.loads(raw)
        doc = value["data"]
        rows = doc["list"]
    except (TypeError, KeyError, ValueError) as exc:
        raise HistoryImportError("malformed KuCoin UTA candle response") from exc
    if value.get("code") != "200000":
        raise HistoryImportError("KuCoin rejected candle query")
    if doc.get("tradeType") != "FUTURES" or doc.get("symbol") != CONTRACTS[asset]:
        raise HistoryImportError("wrong original KuCoin contract/product")
    if not isinstance(rows, list) or len(rows) >= MAX_CANDLES:
        raise HistoryImportError("KuCoin original candle page truncated/invalid")
    period = KLINES[family][2]
    times = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 7:
            raise HistoryImportError("KuCoin candle shape changed")
        ts = row[0]
        if type(ts) is not int or not int(start.timestamp()) <= ts < int(end.timestamp()):
            raise HistoryImportError("outside source UTC time window")
        if ts % period or ts in times:
            raise HistoryImportError("duplicate/misaligned candle timestamp")
        times.add(ts)
        open_, high, low, close = (
            finite_number(item, positive=family != "premium") for item in row[1:5]
        )
        if not low <= min(open_, close) <= max(open_, close) <= high:
            raise HistoryImportError("inconsistent original candle OHLC")
        finite_number(row[5], nonnegative=True)
        finite_number(row[6], nonnegative=True)
    return sorted(times)


def oi_windows(*, interval: str, now: datetime | None = None):
    if interval not in OI_RETENTION_DAYS:
        raise HistoryImportError("unknown source OI retention interval")
    clock = _clock(now).replace(minute=0, second=0, microsecond=0)
    days = OI_RETENTION_DAYS[interval]
    # Four-day source blocks for hourly and 70-day blocks for daily would risk
    # provider pagination; stay safely below 100 observations per request.
    unit = timedelta(days=2 if interval == "1hour" else 60)
    begin = clock - timedelta(days=days - 1)
    while begin < clock:
        right = min(begin + unit, clock)
        yield begin, right
        begin = right


def oi_params(asset: str, interval: str, start: datetime, end: datetime):
    if asset not in ASSETS or interval not in OI_RETENTION_DAYS or not start < end:
        raise HistoryImportError("invalid OI provider scope")
    return {
        "symbol": CONTRACTS[asset], "interval": interval, "pageSize": 100,
        "startAt": int(start.timestamp() * 1000),
        "endAt": int(end.timestamp() * 1000) - 1,
    }


def validate_oi(raw: bytes, *, start: datetime, end: datetime) -> list[int]:
    if not 0 < len(raw) <= MAX_RAW_BYTES:
        raise HistoryImportError("invalid KuCoin original OI response")
    try:
        doc = json.loads(raw)
        rows = doc["data"]
    except (TypeError, KeyError, ValueError) as exc:
        raise HistoryImportError("KuCoin OI response changed") from exc
    if doc.get("code") != "200000":
        raise HistoryImportError("KuCoin OI request rejected")
    if not isinstance(rows, list) or len(rows) >= MAX_OI_ROWS:
        raise HistoryImportError("KuCoin OI page capped/untrusted")
    times = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryImportError("malformed historical OI item")
        ts = row.get("ts")
        if type(ts) is not int or not int(start.timestamp() * 1000) <= ts < int(
            end.timestamp() * 1000
        ):
            raise HistoryImportError("OI timestamp outside source window")
        finite_number(row.get("openInterest"), nonnegative=True)
        if ts in times:
            raise HistoryImportError("duplicated OI reading")
        times.add(ts)
    return sorted(times)


async def get_original(client: httpx.AsyncClient, *, endpoint: str,
                       params: dict) -> tuple[bytes, str]:
    if endpoint not in ("kline", "open-interest"):
        raise HistoryImportError("source endpoint must be allowlisted")
    url = BASE + "/" + endpoint
    async with client.stream("GET", url, params=params,
                             follow_redirects=False) as response:
        if response.status_code != 200:
            raise HistoryImportError(f"KuCoin {endpoint} HTTP {response.status_code}")
        if (response.request.url.host != "api.kucoin.com"
            or response.request.url.path != "/api/ua/v2/market/" + endpoint):
            raise HistoryImportError("KuCoin source endpoint changed")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_RAW_BYTES:
                raise HistoryImportError("original source response oversized")
        return bytes(raw), str(response.request.url)


async def require_private_permissions(drive, file_ids: tuple[str, ...]) -> None:
    """Fail closed if a source folder or original is link/domain shared."""
    headers = await drive._headers()
    for file_id in file_ids:
        if not isinstance(file_id, str) or not file_id:
            raise HistoryImportError("missing Drive file boundary")
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files/{file_id}/permissions",
            params={"fields": "nextPageToken,permissions(id,type,role)",
                    "pageSize": 100},
            headers=headers,
        )
        if response.status_code != 200:
            raise HistoryImportError("Drive privacy could not be verified")
        try:
            payload = response.json()
            permissions = payload["permissions"]
            if (not isinstance(permissions, list)
                or len(permissions) > 100
                or payload.get("nextPageToken")):
                raise HistoryImportError("Drive permission inspection incomplete")
            for item in permissions:
                if (not isinstance(item, dict) or item.get("type") not in
                    ("user", "group")):
                    raise HistoryImportError("Drive source is publicly or domain shared")
        except (TypeError, KeyError, ValueError) as exc:
            raise HistoryImportError("malformed Drive access permissions") from exc


async def private_original(drive, *, root: Path, asset: str, family: str,
                           label: str, start: datetime, end: datetime,
                           raw: bytes, url: str, stamps: list[int]) -> dict:
    if asset not in ASSETS or family not in FAMILIES or not stamps:
        raise HistoryImportError("cannot publish empty/unknown original")
    digest = hashlib.sha256(raw).hexdigest()
    # Immutable per exact UTC slice + source identity; no overwrites/rewrites.
    stamp = start.strftime("%Y%m%dT%H%M%S")
    prefix = f"kucoin-uta-v2-{asset}-{family}-{label}-{stamp}-{digest[:16]}"
    root.mkdir(parents=True, exist_ok=True)
    original = root / (prefix + ".source.json")
    manifest = root / (prefix + ".manifest.json")
    if original.exists() or manifest.exists():
        raise HistoryImportError("source archive must remain immutable")
    original.write_bytes(raw)
    record = {
        "schema_version": "rival-kucoin-uta-v2-private-original-v1",
        "venue": "kucoin", "asset": asset, "contract": CONTRACTS[asset],
        "family": family, "interval": label,
        "requested_utc_start": start.isoformat(),
        "requested_utc_end_exclusive": end.isoformat(),
        "source_url": url, "original_filename": original.name,
        "original_sha256": digest, "source_records": len(stamps),
        "first_timestamp": stamps[0], "last_timestamp": stamps[-1],
        "page_only_not_full_month": True,
        "contract_listing_not_verified": True,
        "historical_retention_may_be_limited": True,
        "private_drive_original_only": True,
        "public_export_permitted": False,
        "model_training_eligible": False,
        "live_model_promoted": False,
    }
    manifest.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    source_item = _artifact(original, len(stamps))
    manifest_item = _artifact(manifest, 1)
    batch = "kucoin-uta-v2-" + hashlib.sha256(
        f"{asset}:{family}:{label}:{stamp}:{digest}".encode()).hexdigest()[:24]
    saved = await drive.ensure_history_file(
        artifact=source_item, batch_id=batch, mime_type="application/json")
    saved_meta = await drive.ensure_manifest_file(
        artifact=manifest_item, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history,
        artifact=source_item)
    await drive.verify_downloaded_artifact(
        file_id=saved_meta.file_id, parent_id=drive.folders.manifests,
        artifact=manifest_item)
    await require_private_permissions(drive, (saved.file_id, saved_meta.file_id))
    return {"asset": asset, "family": family, "interval": label,
            "original_rows": len(stamps), "original_sha256": digest,
            "readback_verified": True}


async def collect(*, family: str, month: str = "auto", limit: int = 48,
                  out_dir: Path = Path("data/kucoin-uta-v2")) -> dict:
    if family not in FAMILIES or not 1 <= limit <= 64:
        raise HistoryImportError("unsupported or unbounded source research")
    clock = datetime.now(UTC)
    if family in KLINES:
        month = select_month(family, now=clock) if month == "auto" else month
        begin, end = bounds(month, family, now=clock)
    elif month != "auto":
        raise HistoryImportError("OI retains recent windows only; historical month forbidden")
    drive = _drive(Settings())
    receipts = []
    blocked = []
    attempts = 0
    try:
        await drive._verify_folder_boundary()
        await require_private_permissions(
            drive, (drive.folders.root, drive.folders.history,
                    drive.folders.manifests, drive.folders.staging))
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            jobs = []
            if family in KLINES:
                _kind, interval, seconds = KLINES[family]
                windows = list(split_month(begin, end, seconds))
                # Allocate pages evenly across all assets, rotate windows on
                # successive runs instead of repeatedly scraping page zero.
                per_asset = max(1, limit // len(ASSETS))
                offset = ((int(clock.timestamp()) // (6 * 3600) // len(KLINES)) * per_asset) % len(windows)
                chosen = [windows[(offset + i) % len(windows)]
                          for i in range(min(per_asset, len(windows)))]
                jobs = [(asset, interval, a, b) for asset in ASSETS
                        for a, b in chosen]
            else:
                # Never ask the source for unavailable 2021 OI.
                jobs = [(asset, interval, a, b) for asset in ASSETS
                        for interval in OI_RETENTION_DAYS
                        for a, b in oi_windows(interval=interval, now=clock)]
            for asset, interval, start, end in jobs[:limit]:
                attempts += 1
                try:
                    if family in KLINES:
                        params = kline_params(family, asset, start, end)
                        raw, url = await get_original(
                            client, endpoint="kline", params=params)
                        times = validate_klines(raw, family=family, asset=asset,
                                                start=start, end=end)
                    else:
                        params = oi_params(asset, interval, start, end)
                        raw, url = await get_original(
                            client, endpoint="open-interest", params=params)
                        times = validate_oi(raw, start=start, end=end)
                    if times:
                        receipts.append(await private_original(
                            drive, root=out_dir, asset=asset, family=family,
                            label=interval, start=start, end=end, raw=raw,
                            url=url, stamps=times))
                except (HistoryImportError, httpx.HTTPError, OSError,
                        RuntimeError) as exc:
                    reason = str(exc)[:160]
                    blocked.append({"asset": asset, "family": family,
                                    "interval": interval, "reason": reason})
                    if any(f"HTTP {code}" in reason for code in (403, 429, 500, 502, 503, 504)):
                        break  # No hammering a denied or rate-limited API.
                await asyncio.sleep(0.4)
    finally:
        await drive.aclose()
    report = {
        "schema_version": "rival-kucoin-uta-v2-private-research-v1",
        "family": family, "source_month": month if family in KLINES else None,
        "attempted_windows": attempts,
        "original_segments_with_drive_readback": len(receipts),
        "total_original_rows": sum(x["original_rows"] for x in receipts),
        "blocked_windows": blocked[:50],
        "all_calendar_history_complete": False,
        "source_listing_proven": False,
        "model_training_approved": False,
        "public_raw_artifacts_uploaded": False,
    }
    report_path = Path(f"data/native-reports/kucoin-uta-v2-{family}-progress.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Private KuCoin original family acquisition")
    parser.add_argument("--family", choices=("auto",) + FAMILIES, default="auto")
    parser.add_argument("--month", default="auto")
    parser.add_argument("--limit", type=int, default=48)
    args = parser.parse_args()
    family = choose_family() if args.family == "auto" else args.family
    try:
        report = asyncio.run(collect(
            family=family, month=args.month, limit=args.limit))
    except (HistoryImportError, httpx.HTTPError, OSError, RuntimeError) as exc:
        parser.exit(2, f"KuCoin original source blocked: {exc}\n")
    print(json.dumps(report, sort_keys=True))
    return 2 if report["blocked_windows"] and report["original_segments_with_drive_readback"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
