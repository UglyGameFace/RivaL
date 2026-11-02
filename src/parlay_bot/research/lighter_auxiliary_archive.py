"""Lighter original public funding and mark-price archives, research-only."""
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

BASE = "https://mainnet.zklighter.elliot.ai/api/v1/"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
MAX_BYTES = 3_000_000


def month_bounds(month: str) -> tuple[datetime, datetime]:
    if (len(month) != 7 or month[4] != "-" or not month[:4].isascii()
        or not month[5:].isascii() or not month[:4].isdecimal()
        or not month[5:].isdecimal()):
        raise HistoryImportError("invalid Lighter historical calendar month")
    try:
        start = datetime(int(month[:4]), int(month[5:]), 1, tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid Lighter source month") from exc
    current = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if not datetime(2025, 2, 1, tzinfo=UTC) <= start < current:
        raise HistoryImportError("month not available before Lighter market launch")
    return start, (start + timedelta(days=32)).replace(day=1)


def automatic_month(now: datetime | None = None) -> str:
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    first = datetime(2025, 2, 1, tzinfo=UTC)
    current = clock.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = (current.year - first.year) * 12 + current.month - first.month
    if months <= 0:
        raise HistoryImportError("no finished Lighter research month")
    offset = (int(clock.timestamp()) // (6 * 3600)) % months
    year, month_index = divmod(2025 * 12 + 1 + offset, 12)
    return f"{year:04d}-{month_index + 1:02d}"


async def fetch_source(client, endpoint: str, params: dict | None = None):
    if endpoint not in ("orderBooks", "fundings", "markPriceCandles"):
        raise HistoryImportError("untrusted Lighter public route")
    async with client.stream("GET", BASE + endpoint, params=params,
                             follow_redirects=False) as response:
        if response.status_code != 200:
            raise HistoryImportError(f"Lighter {endpoint} HTTP {response.status_code}")
        if (response.request.url.host != "mainnet.zklighter.elliot.ai"
            or response.request.url.path != "/api/v1/" + endpoint):
            raise HistoryImportError("Lighter public source changed identity")
        buf = bytearray()
        async for chunk in response.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_BYTES:
                raise HistoryImportError("Lighter source body too large")
        return bytes(buf), str(response.request.url)


def active_ids(raw: bytes) -> dict[str, int]:
    try:
        payload = json.loads(raw)
        rows = payload["order_books"]
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoryImportError("invalid original Lighter perpetual catalog") from exc
    if payload.get("code") != 200 or not isinstance(rows, list):
        raise HistoryImportError("Lighter original market catalog unavailable")
    found, occupied = {}, set()
    for item in rows:
        if not isinstance(item, dict):
            raise HistoryImportError("malformed original Lighter market")
        if item.get("market_type") != "perp" or item.get("status") != "active":
            continue
        symbol, mid = item.get("symbol"), item.get("market_id")
        if symbol in ASSETS:
            if (type(mid) is not int or mid < 0 or mid in occupied
                or symbol in found):
                raise HistoryImportError("ambiguous Lighter market ID")
            found[symbol] = mid
            occupied.add(mid)
    return found


def positive(value: object, *, allow_zero=False) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryImportError("invalid Lighter numeric field") from exc
    if isinstance(value, bool) or not math.isfinite(n) or n < 0 or (not allow_zero and n == 0):
        raise HistoryImportError("invalid historical Lighter amount or price")
    return n


def validate(raw: bytes, *, family: str, begin: datetime, end: datetime):
    if not 0 < len(raw) <= MAX_BYTES:
        raise HistoryImportError("empty or oversized original Lighter response")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HistoryImportError("Lighter original source JSON invalid") from exc
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise HistoryImportError("Lighter original source rejected")
    if family == "funding":
        rows = payload.get("fundings")
        cap, key, divisor = 750, "timestamp", 1
    elif family == "mark_price":
        rows = payload.get("c", payload.get("candles"))
        cap, key, divisor = 500, "t", 1000
    else:
        raise HistoryImportError("unexpected Lighter data family")
    if not isinstance(rows, list) or len(rows) > cap:
        raise HistoryImportError("unsupported or oversized Lighter page schema")
    lo, hi = int(begin.timestamp()), int(end.timestamp())
    times = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryImportError("malformed original Lighter row")
        stamp = row.get(key)
        if (isinstance(stamp, bool) or not isinstance(stamp, (str, int))
            or not str(stamp).isascii() or not str(stamp).isdecimal()):
            raise HistoryImportError("Lighter source timestamp unit ambiguous")
        epoch = int(stamp) // divisor
        if not lo <= epoch < hi or epoch in times:
            raise HistoryImportError("Lighter source off-window/duplicated timestamp")
        times.add(epoch)
        if family == "funding":
            if row.get("direction") not in ("long", "short"):
                raise HistoryImportError("unknown funding direction")
            if positive(row.get("rate"), allow_zero=True) > 1:
                raise HistoryImportError("unverified funding magnitude")
            positive(row.get("value"), allow_zero=True)
        else:
            o, h, l, c = (positive(row.get(k)) for k in ("o", "h", "l", "c"))
            if l > min(o, c) or h < max(o, c):
                raise HistoryImportError("invalid mark price source OHLC")
    return times, len(rows)


async def store(drive, *, raw: bytes, asset: str, family: str,
                month: str, section: int, market_id: int, url: str, rows: int):
    digest = hashlib.sha256(raw).hexdigest()
    stem = f"lighter-{asset}-{family}-{month}-{section:02d}-{digest[:16]}"
    root = Path("data/lighter-original-auxiliary")
    root.mkdir(parents=True, exist_ok=True)
    source = root / (stem + ".source.json")
    meta = root / (stem + ".manifest.json")
    if source.exists() or meta.exists():
        raise HistoryImportError("refusing to replace original Lighter research")
    source.write_bytes(raw)
    meta.write_text(json.dumps({
        "schema": "rival-lighter-original-auxiliary-v1",
        "venue": "lighter", "asset": asset, "market_id": market_id,
        "quote": "USDC", "product": "perpetual", "family": family,
        "utc_month": month, "section": section,
        "source_url": url, "source_filename": source.name,
        "original_sha256": digest, "records": rows,
        "source_provider_checksum_available": False,
        "prospective_forecast": False, "model_admission": False,
        "complete_source_month_claimed": False,
    }, sort_keys=True, indent=2) + "\n")
    first, second = _artifact(source, rows), _artifact(meta, 1)
    batch = "lighter-native-" + hashlib.sha256(
        f"{asset}:{family}:{month}:{section}:{digest}".encode()
    ).hexdigest()[:24]
    original = await drive.ensure_history_file(
        artifact=first, batch_id=batch, mime_type="application/json")
    manifest = await drive.ensure_manifest_file(
        artifact=second, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=original.file_id, parent_id=drive.folders.history, artifact=first)
    await drive.verify_downloaded_artifact(
        file_id=manifest.file_id, parent_id=drive.folders.manifests, artifact=second)
    return {"asset": asset, "family": family, "section": section, "rows": rows,
            "original_sha256": digest, "drive_bytes_verified": True}


async def collect(*, month: str, assets: tuple[str, ...]):
    start, end = month_bounds(month)
    if not assets or len(set(assets)) != len(assets) or any(a not in ASSETS for a in assets):
        raise HistoryImportError("unbounded Lighter original data scope")
    drive = _drive(Settings())
    outputs, blocked = [], []
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            catalog, _ = await fetch_source(client, "orderBooks")
            ids = active_ids(catalog)
            for asset in assets:
                if asset not in ids:
                    blocked.append({"asset": asset, "reason": "no active native market mapping"})
                    continue
                for family in ("funding", "mark_price"):
                    periods = [(start, end)] if family == "funding" else [
                        (start, min(start + timedelta(days=15), end)),
                        (min(start + timedelta(days=15), end), end),
                    ]
                    seen = set()
                    for index, (first, last) in enumerate(periods):
                        params = {"market_id": ids[asset], "resolution": "1h",
                                  "start_timestamp": int(first.timestamp()),
                                  "end_timestamp": int(last.timestamp()),
                                  "count_back": 750 if family == "funding" else 500}
                        endpoint = "fundings" if family == "funding" else "markPriceCandles"
                        try:
                            raw, url = await fetch_source(client, endpoint, params)
                            times, rows = validate(raw, family=family, begin=first, end=last)
                            if seen.intersection(times):
                                raise HistoryImportError("original source page overlap")
                            seen.update(times)
                            if not rows:
                                blocked.append({"asset": asset, "family": family,
                                                "section": index, "reason": "no original rows"})
                            else:
                                outputs.append(await store(
                                    drive, raw=raw, asset=asset, family=family,
                                    month=month, section=index, market_id=ids[asset],
                                    url=url, rows=rows))
                        except (HistoryImportError, httpx.HTTPError, OSError) as exc:
                            blocked.append({"asset": asset, "family": family,
                                            "section": index, "reason": str(exc)[:180]})
                            if "HTTP 429" in str(exc):
                                break
                        await asyncio.sleep(1.1)
                    hours = int((end - start).total_seconds() // 3600)
                    buckets = {ts // 3600 for ts in seen}
                    outputs.append({
                        "asset": asset, "family": family, "month": month,
                        "observed_source_hours": len(seen), "expected_hours": hours,
                        "full_utc_hourly_coverage": (
                            len(buckets) == hours and len(seen) == hours
                            and all(int(start.timestamp()) // 3600 + i in buckets
                                    for i in range(hours))
                        ),
                    })
    finally:
        await drive.aclose()
    report = {
        "schema": "rival-lighter-original-funding-mark-v1", "month": month,
        "results": outputs, "blocked": blocked,
        "source_readbacks_verified": sum(
            x.get("drive_bytes_verified") is True for x in outputs),
        "all_lighter_history_complete": False, "live_models_changed": False,
    }
    dest = Path("data/native-reports/lighter-original-funding-mark.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default="2025-02")
    parser.add_argument("--assets", nargs="+", choices=ASSETS, default=["BTC", "ETH"])
    args = parser.parse_args()
    try:
        month = automatic_month() if args.month == "auto" else args.month
        report = asyncio.run(collect(month=month, assets=tuple(args.assets)))
    except (HistoryImportError, httpx.HTTPError, RuntimeError, OSError) as exc:
        parser.exit(2, f"Lighter source-native auxiliary BLOCKED: {exc}\n")
    print(json.dumps({
        "month": month, "readback_sources": report["source_readbacks_verified"],
        "blocked": report["blocked"], "full_history_complete": False,
    }, sort_keys=True))
    return 0 if report["source_readbacks_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
