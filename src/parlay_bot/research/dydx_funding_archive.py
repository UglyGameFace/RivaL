"""Archive original dYdX v4 historical funding + oracle reference prices.

This is independent offline research. Never treat old funding as a future signal,
or v4 chain records as legacy v3. Preserve every downloaded API response in
private RivaL Drive with SHA-256 readback and a deterministic source receipt.
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

BASE = "https://indexer.dydx.trade/v4/historicalFunding/"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
LIMIT = 100
MAX_PAGES = 10
MAX_RESPONSE = 2_000_000


def month_bounds(month: str) -> tuple[datetime, datetime]:
    if (len(month) != 7 or month[4] != "-"
        or not month[:4].isascii() or not month[5:].isascii()
        or not month[:4].isdecimal() or not month[5:].isdecimal()):
        raise HistoryImportError("invalid funding UTC month")
    try:
        start = datetime(int(month[:4]), int(month[5:]), 1, tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid funding month") from exc
    end = (start + timedelta(days=32)).replace(day=1)
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0,
                                    second=0, microsecond=0)
    if not datetime(2023, 11, 1, tzinfo=UTC) <= start < now:
        raise HistoryImportError("dYdX v4 not live before Nov 2023")
    return start, end


def parse_time(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise HistoryImportError("invalid UTC funding timestamp")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HistoryImportError("invalid funding timestamp") from exc
    if value.utcoffset() != timedelta(0):
        raise HistoryImportError("dYdX funding timestamp not UTC")
    return value.astimezone(UTC)


def validate_page(raw: bytes, *, asset: str, cursor: datetime,
                  start: datetime, end: datetime) -> list[datetime]:
    if asset not in ASSETS or not 0 < len(raw) <= MAX_RESPONSE:
        raise HistoryImportError("invalid dYdX source page")
    try:
        payload = json.loads(raw)
        events = payload["historicalFunding"]
    except (ValueError, TypeError, KeyError) as exc:
        raise HistoryImportError("malformed dYdX funding response") from exc
    if not isinstance(events, list) or len(events) > LIMIT:
        raise HistoryImportError("dYdX funding page exceeds request limit")
    stamps = []
    prior = cursor
    for item in events:
        if not isinstance(item, dict):
            raise HistoryImportError("invalid funding record")
        if item.get("ticker") != asset + "-USD":
            raise HistoryImportError("wrong historical dYdX ticker")
        ts = parse_time(item.get("effectiveAt"))
        if not ts <= prior or ts >= end:
            raise HistoryImportError("future, duplicated or misordered funding")
        if stamps and ts >= stamps[-1]:
            raise HistoryImportError("duplicated/unordered funding timestamp")
        for key in ("rate", "price"):
            try:
                num = float(item[key])
            except (ValueError, OverflowError, TypeError, KeyError) as exc:
                raise HistoryImportError("missing/invalid funding price or rate") from exc
            if not math.isfinite(num) or (
                key == "price" and num <= 0
            ) or (key == "rate" and abs(num) > 1):
                raise HistoryImportError("untrustworthy dYdX funding numerical value")
        stamps.append(ts)
        prior = ts
    return stamps


async def archive_page(drive, *, root: Path, asset: str, month: str,
                       page: int, raw: bytes, url: str,
                       stamps: list[datetime]) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    stem = f"dydx-v4-{asset}-USD-funding-{month}-{page:02d}-{digest[:16]}"
    root.mkdir(parents=True, exist_ok=True)
    source = root / (stem + ".source.json")
    metadata = root / (stem + ".manifest.json")
    if source.exists() or metadata.exists():
        raise HistoryImportError("cannot overwrite original dYdX proof")
    source.write_bytes(raw)
    meta = {
        "schema_version": "rival-dydx-v4-original-funding-page-v1",
        "venue": "dydx", "chain_version": "v4",
        "asset": asset, "contract": asset + "-USD",
        "family": "funding_with_oracle_price", "utc_month": month,
        "page": page, "source_url": url,
        "source_filename": source.name, "original_sha256": digest,
        "records": len(stamps),
        "latest_timestamp": stamps[0].isoformat() if stamps else None,
        "earliest_timestamp": stamps[-1].isoformat() if stamps else None,
        "original_provider_checksum_available": False,
        "single_page_not_full_month": True,
        "model_training_eligible": False, "live_signals_affected": False,
    }
    metadata.write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n")
    original_artifact = _artifact(source, len(stamps))
    manifest_artifact = _artifact(metadata, 1)
    batch = "dydx-v4-fund-" + hashlib.sha256(
        f"{asset}:{month}:{page}:{digest}".encode()
    ).hexdigest()[:24]
    saved = await drive.ensure_history_file(
        artifact=original_artifact, batch_id=batch, mime_type="application/json")
    recorded = await drive.ensure_manifest_file(
        artifact=manifest_artifact, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history,
        artifact=original_artifact)
    await drive.verify_downloaded_artifact(
        file_id=recorded.file_id, parent_id=drive.folders.manifests,
        artifact=manifest_artifact)
    return {"asset": asset, "month": month, "page": page, "records": len(stamps),
            "original_sha256": digest, "readback_verified": True}


async def acquire_asset(drive, client: httpx.AsyncClient, *, asset: str,
                        month: str, root: Path) -> dict:
    start, end = month_bounds(month)
    cursor = end - timedelta(milliseconds=1)
    seen = set()
    pages = []
    crossed_start = False
    for number in range(MAX_PAGES):
        params = {"effectiveBeforeOrAt": cursor.isoformat().replace("+00:00", "Z"),
                  "limit": LIMIT}
        async with client.stream(
            "GET", BASE + asset + "-USD", params=params, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise HistoryImportError(
                    f"dYdX v4 funding API HTTP {response.status_code}")
            if (response.request.url.host != "indexer.dydx.trade"
                or response.request.url.path != "/v4/historicalFunding/" + asset + "-USD"):
                raise HistoryImportError("historical funding source changed identity")
            raw = bytearray()
            async for block in response.aiter_bytes():
                raw.extend(block)
                if len(raw) > MAX_RESPONSE:
                    raise HistoryImportError("funding page exceeds safe byte limit")
            url = str(response.request.url)
        stamps = validate_page(bytes(raw), asset=asset, cursor=cursor,
                               start=start, end=end)
        if not stamps:
            break
        if any(t in seen for t in stamps):
            raise HistoryImportError("funding page overlaps prior original page")
        seen.update(stamps)
        pages.append(await archive_page(
            drive, root=root, asset=asset, month=month, page=number,
            raw=bytes(raw), url=url, stamps=stamps))
        if stamps[-1] < start:
            crossed_start = True
            break
        cursor = stamps[-1] - timedelta(milliseconds=1)
        await asyncio.sleep(0.5)
    expected = int((end - start).total_seconds() // 3600)
    in_month = sorted(t for t in seen if start <= t < end)
    full = crossed_start and len(in_month) == expected and all(
        stamp == start + timedelta(hours=i) for i, stamp in enumerate(in_month)
    )
    return {
        "asset": asset, "month": month, "pages": pages,
        "source_events_in_month": len(in_month), "expected_hourly_events": expected,
        "crossed_month_boundary": crossed_start,
        "full_utc_month_hourly_coverage": full,
        "truncated_or_unavailable": not crossed_start,
        "raw_page_readbacks_verified": len(pages),
        "historic_model_eligible": False,
    }


async def acquire(*, month: str, assets: tuple[str, ...],
                  root: Path = Path("data/dydx-v4-original-funding")) -> dict:
    month_bounds(month)
    if not assets or len(assets) > 4 or len(assets) != len(set(assets)) or any(
        a not in ASSETS for a in assets
    ):
        raise HistoryImportError("invalid dYdX funded contract scope")
    drive = _drive(Settings())
    results = []
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            for asset in assets:
                results.append(await acquire_asset(
                    drive, client, asset=asset, month=month, root=root))
    finally:
        await drive.aclose()
    report = {
        "schema_version": "rival-dydx-v4-original-funding-month-v1",
        "month": month, "assets": results,
        "original_source_pages_readback_verified": sum(
            item["raw_page_readbacks_verified"] for item in results),
        "all_venue_family_history_complete": False,
        "live_model_promoted": False,
    }
    output = Path("data/native-reports/dydx-v4-original-funding.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default="2023-11")
    parser.add_argument("--assets", nargs="+", choices=ASSETS,
                        default=["BTC", "ETH"])
    args = parser.parse_args()
    try:
        result = asyncio.run(acquire(month=args.month, assets=tuple(args.assets)))
    except (HistoryImportError, httpx.HTTPError, OSError,
            RuntimeError, ValueError) as exc:
        parser.exit(2, f"dYdX source-native funding BLOCKED: {exc}\n")
    print(json.dumps({
        "month": result["month"], "assets": [{
            "asset": x["asset"], "events": x["source_events_in_month"],
            "verified_pages": x["raw_page_readbacks_verified"],
            "full_hourly_coverage": x["full_utc_month_hourly_coverage"],
            "truncated_or_unavailable": x["truncated_or_unavailable"],
        } for x in result["assets"]],
        "complete_2021_to_present": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
