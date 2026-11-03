"""Original public funding-history pages from four independent perpetual exchanges.

Bounded source requests. Every retained source is uploaded as original bytes with
an immutable manifest and independently downloaded SHA-256 verification. Never
infer contract listing, full-month hourly coverage or model eligibility from a
page (including a page that returns HTTP 200).
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

ASSETS = ("BTC", "ETH", "SOL", "XRP")
VENUES = ("hyperliquid", "kucoin", "phemex", "deribit")
FIRST_ATTEMPT = {"hyperliquid": "2023-01", "kucoin": "2021-01",
                 "phemex": "2021-01", "deribit": "2021-01"}
URLS = {
    "hyperliquid": "https://api.hyperliquid.xyz/info",
    "kucoin": "https://api-futures.kucoin.com/api/v1/contract/funding-rates",
    "phemex": "https://api.phemex.com/api-data/public/data/funding-rate-history",
    "deribit": "https://www.deribit.com/api/v2/public/get_funding_rate_history",
}
KUCOIN_SYMBOLS = {"BTC": "XBTUSDTM", "ETH": "ETHUSDTM",
                  "SOL": "SOLUSDTM", "XRP": "XRPUSDTM"}
DERIBIT_SYMBOLS = {"BTC": "BTC-PERPETUAL", "ETH": "ETH-PERPETUAL",
                   "SOL": "SOL_USDC-PERPETUAL", "XRP": "XRP_USDC-PERPETUAL"}
MAX_BYTES = 2_000_000
MAX_RECORDS = 500
WINDOW_DAYS = 4


def month_bounds(venue: str, month: str, *, now: datetime | None = None):
    """Completed UTC month, not a claimed exchange/instrument listing date."""
    if venue not in VENUES or not isinstance(month, str) or len(month) != 7 or (
        month[4] != "-" or not month[:4].isascii() or not month[5:].isascii()
        or not month[:4].isdecimal() or not month[5:].isdecimal()
    ):
        raise HistoryImportError("invalid funding venue/month")
    try:
        start = datetime(int(month[:4]), int(month[5:]), 1, tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid funding UTC month") from exc
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    if not FIRST_ATTEMPT[venue] <= month < current.strftime("%Y-%m"):
        raise HistoryImportError("funding month outside conservative attempt range")
    return start, (start + timedelta(days=32)).replace(day=1)


def automatic_month(venue: str, now: datetime | None = None) -> str:
    if venue not in VENUES:
        raise HistoryImportError("unrecognized funding venue")
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    current = clock.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    yy, mm = map(int, FIRST_ATTEMPT[venue].split("-"))
    months = []
    while (yy, mm) < (current.year, current.month):
        months.append(f"{yy:04d}-{mm:02d}")
        yy, mm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
    if not months:
        raise HistoryImportError("no completed historical month")
    # Prioritize the newest 24 completed months, then rotate every older month.
    ordered = list(reversed(months[-24:])) + months[:-24]
    return ordered[clock.date().toordinal() % len(ordered)]


def segments(start: datetime, end: datetime):
    """Disjoint four-day UTC windows so a 500-record API cap cannot hide an hour."""
    cursor = start
    while cursor < end:
        following = min(cursor + timedelta(days=WINDOW_DAYS), end)
        yield cursor, following
        cursor = following


def _ms(stamp: datetime) -> int:
    return int(stamp.timestamp() * 1000)


def contract_for(venue: str, asset: str) -> str:
    if venue not in VENUES or asset not in ASSETS:
        raise HistoryImportError("untrusted historical contract")
    if venue == "hyperliquid":
        return asset
    if venue == "kucoin":
        return KUCOIN_SYMBOLS[asset]
    if venue == "phemex":
        return f".{asset}USDTFR8H"
    return DERIBIT_SYMBOLS[asset]


def request_spec(venue: str, asset: str, start: datetime, end: datetime):
    if venue not in VENUES or asset not in ASSETS or not start < end:
        raise HistoryImportError("untrusted funding query scope")
    left, right = _ms(start), _ms(end) - 1
    if venue == "hyperliquid":
        return "POST", URLS[venue], {"json": {
            "type": "fundingHistory", "coin": asset,
            "startTime": left, "endTime": right}}
    if venue == "kucoin":
        return "GET", URLS[venue], {"params": {
            "symbol": contract_for(venue, asset), "from": left, "to": right}}
    if venue == "phemex":
        return "GET", URLS[venue], {"params": {
            "symbol": contract_for(venue, asset), "start": left,
            "end": right, "limit": 100}}
    return "GET", URLS[venue], {"params": {
        "instrument_name": contract_for(venue, asset),
        "start_timestamp": left, "end_timestamp": right}}


def _finite_rate(value: object) -> float:
    if isinstance(value, bool):
        raise HistoryImportError("boolean funding amount")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HistoryImportError("non-numeric funding rate") from exc
    if not math.isfinite(number) or abs(number) > 1:
        raise HistoryImportError("invalid original funding magnitude")
    return number


def validate_page(raw: bytes, *, venue: str, asset: str,
                  start: datetime, end: datetime) -> list[int]:
    if venue not in VENUES or asset not in ASSETS or not 0 < len(raw) <= MAX_BYTES:
        raise HistoryImportError("invalid original funding response")
    try:
        payload = json.loads(raw)
        if venue == "hyperliquid":
            rows = payload
        else:
            if not isinstance(payload, dict) or payload.get("code") != "200000":
                raise HistoryImportError("KuCoin funding endpoint rejected request")
            rows = payload["data"]
    except (ValueError, TypeError, KeyError) as exc:
        raise HistoryImportError("malformed original funding JSON") from exc
    # Exact cap must fail closed: provider may have truncated the requested
    # interval; never mislabel the result as a complete UTC window.
    if not isinstance(rows, list) or len(rows) >= MAX_RECORDS:
        raise HistoryImportError("funding response invalid or provider-capped")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryImportError("funding record is not an object")
        symbol = row.get("coin") if venue == "hyperliquid" else row.get("symbol")
        if symbol != (asset if venue == "hyperliquid" else KUCOIN_SYMBOLS[asset]):
            raise HistoryImportError("mismatched official funding contract")
        stamp = row.get("time") if venue == "hyperliquid" else row.get("timepoint")
        if type(stamp) is not int or not _ms(start) <= stamp < _ms(end):
            raise HistoryImportError("funding timestamp outside exact UTC window")
        if stamp in seen:
            raise HistoryImportError("duplicate original funding observation")
        seen.add(stamp)
        _finite_rate(row.get("fundingRate"))
        if venue == "hyperliquid" and row.get("premium") is not None:
            _finite_rate(row["premium"])
    return sorted(seen)


async def fetch_page(client: httpx.AsyncClient, *, venue: str, asset: str,
                     start: datetime, end: datetime) -> tuple[bytes, str]:
    method, url, kwargs = request_spec(venue, asset, start, end)
    async with client.stream(method, url, follow_redirects=False,
                             **kwargs) as response:
        if response.status_code != 200:
            raise HistoryImportError(
                f"{venue} official funding HTTP {response.status_code}")
        expected = httpx.URL(url)
        if (response.request.url.host != expected.host or
            response.request.url.path != expected.path):
            raise HistoryImportError("funding source URL changed identity")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HistoryImportError("original funding response too large")
        return bytes(raw), str(response.request.url)


async def archive_page(drive, *, root: Path, venue: str, asset: str,
                       month: str, part: int, start: datetime, end: datetime,
                       raw: bytes, source_url: str, stamps: list[int]) -> dict:
    if not stamps:
        raise HistoryImportError("empty pages are not complete source receipts")
    digest = hashlib.sha256(raw).hexdigest()
    prefix = f"{venue}-{asset}-funding-{month}-{part:02d}-{digest[:16]}"
    root.mkdir(parents=True, exist_ok=True)
    original = root / (prefix + ".source.json")
    manifest = root / (prefix + ".manifest.json")
    if original.exists() or manifest.exists():
        raise HistoryImportError("cannot overwrite immutable funding source")
    original.write_bytes(raw)
    evidence = {
        "schema_version": "rival-official-public-funding-page-v1",
        "venue": venue, "asset": asset, "family": "funding",
        "source_endpoint": URLS[venue], "source_url": source_url,
        "contract": asset if venue == "hyperliquid" else KUCOIN_SYMBOLS[asset],
        "utc_month": month, "part": part,
        "start_utc": start.isoformat(), "end_exclusive_utc": end.isoformat(),
        "original_filename": original.name, "original_sha256": digest,
        "records": len(stamps), "earliest_time_ms": stamps[0],
        "latest_time_ms": stamps[-1],
        "provider_checksum_available": False,
        "partial_original_page_only": True,
        "complete_calendar_month_verified": False,
        "contract_listing_date_verified": False,
        "model_training_eligible": False, "live_model_promoted": False,
    }
    manifest.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    a = _artifact(original, len(stamps))
    b = _artifact(manifest, 1)
    batch = "official-funding-" + hashlib.sha256(
        f"{venue}:{asset}:{month}:{part}:{digest}".encode()).hexdigest()[:24]
    saved = await drive.ensure_history_file(
        artifact=a, batch_id=batch, mime_type="application/json")
    recorded = await drive.ensure_manifest_file(artifact=b, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history, artifact=a)
    await drive.verify_downloaded_artifact(
        file_id=recorded.file_id, parent_id=drive.folders.manifests, artifact=b)
    return {"part": part, "rows": len(stamps), "sha256": digest,
            "drive_original_and_manifest_readback": True}


async def acquire_asset(drive, client: httpx.AsyncClient, *, venue: str,
                        asset: str, month: str, root: Path) -> dict:
    start, end = month_bounds(venue, month)
    result = {"venue": venue, "asset": asset, "month": month,
              "source_parts": [], "empty_source_windows": 0,
              "full_month_funding_coverage_proven": False,
              "model_training_eligible": False}
    for part, (left, right) in enumerate(segments(start, end)):
        raw, url = await fetch_page(
            client, venue=venue, asset=asset, start=left, end=right)
        stamps = validate_page(
            raw, venue=venue, asset=asset, start=left, end=right)
        if stamps:
            result["source_parts"].append(await archive_page(
                drive, root=root, venue=venue, asset=asset, month=month,
                part=part, start=left, end=right, raw=raw, source_url=url,
                stamps=stamps))
        else:
            result["empty_source_windows"] += 1
        await asyncio.sleep(0.35)
    result["requested_windows_completed"] = (
        len(result["source_parts"]) + result["empty_source_windows"])
    return result


async def acquire(*, venue: str, month: str, assets: tuple[str, ...],
                  root: Path = Path("data/official-funding")) -> dict:
    month_bounds(venue, month)
    if not assets or len(assets) > 4 or len(set(assets)) != len(assets) or any(
        a not in ASSETS for a in assets
    ):
        raise HistoryImportError("untrusted asset list")
    drive = _drive(Settings())
    completed, blocked = [], []
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            for asset in assets:
                try:
                    completed.append(await acquire_asset(
                        drive, client, venue=venue, asset=asset, month=month,
                        root=root))
                except (HistoryImportError, httpx.HTTPError, OSError,
                        RuntimeError) as exc:
                    reason = str(exc)[:180]
                    blocked.append({"asset": asset, "reason": reason,
                                    "full_month_coverage_proven": False})
                    if any(f"HTTP {code}" in reason for code in
                           (403, 429, 500, 502, 503, 504)):
                        break  # Protect provider on denied/throttled errors.
    finally:
        await drive.aclose()
    report = {
        "schema_version": "rival-official-public-funding-run-v1",
        "venue": venue, "utc_month": month, "assets": completed,
        "blocked_assets": blocked,
        "raw_parts_with_drive_sha_readback": sum(
            len(r["source_parts"]) for r in completed),
        "full_source_family_history_verified": False,
        "all_models_unchanged": True,
    }
    path = Path("data/native-reports/official-public-funding.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Official public funding originals")
    parser.add_argument("--venue", required=True, choices=VENUES)
    parser.add_argument("--month", default="auto")
    parser.add_argument("--assets", nargs="+", choices=ASSETS, default=list(ASSETS))
    args = parser.parse_args()
    try:
        month = automatic_month(args.venue) if args.month == "auto" else args.month
        report = asyncio.run(acquire(
            venue=args.venue, month=month, assets=tuple(args.assets)))
    except (HistoryImportError, httpx.HTTPError, OSError, RuntimeError,
            ValueError) as exc:
        parser.exit(2, f"Original funding collector blocked: {exc}\n")
    print(json.dumps({
        "venue": args.venue, "month": month,
        "original_pages_drive_sha_verified": report["raw_parts_with_drive_sha_readback"],
        "blocked_assets": report["blocked_assets"],
        "full_history_verified": False,
    }, sort_keys=True))
    return 2 if report["blocked_assets"] and not report["assets"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
