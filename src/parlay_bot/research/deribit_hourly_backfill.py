"""Bounded historical Deribit hourly trade-source recovery.

This extends the verified 2021 minute proof, never altering live forecasts.
Use exact original time ranges and split overflow pages rather than dropping
trades when Deribit's has_more flag is true.
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

HOST = "https://history.deribit.com"
URL = HOST + "/api/v2/public/get_last_trades_by_instrument_and_time"
ASSETS = ("BTC", "ETH")
PAGE_LIMIT = 10000
MAX_SOURCE_BYTES = 9_000_000
MAX_REQUESTS = 48
MIN_SPLIT_MS = 1_000
REQUEST_SPACING = 0.35


def hour_range(day: str, hour: int) -> tuple[int, int]:
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid historical Deribit UTC day") from exc
    start += timedelta(hours=hour)
    end = start + timedelta(hours=1)
    if (not 0 <= hour < 24 or start < datetime(2021, 1, 1, tzinfo=UTC)
        or end > datetime.now(UTC) - timedelta(minutes=5)):
        raise HistoryImportError("historical source hour not fully elapsed")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def inspect_page(
    raw: bytes, *, asset: str, start_ms: int, end_ms: int,
) -> tuple[list[dict], bool]:
    if asset not in ASSETS or not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise HistoryImportError("untrusted Deribit source response")
    try:
        doc = json.loads(raw)
        result = doc["result"]
        rows = result["trades"]
        more = result["has_more"]
    except (KeyError, ValueError, TypeError) as exc:
        raise HistoryImportError("invalid native Deribit source JSON") from exc
    if (not isinstance(rows, list) or len(rows) > PAGE_LIMIT
        or type(more) is not bool):
        raise HistoryImportError("invalid or excessive native Deribit trade page")
    seen: set[str] = set()
    last = start_ms
    for record in rows:
        if not isinstance(record, dict):
            raise HistoryImportError("invalid native trade")
        ts, ident = record.get("timestamp"), record.get("trade_id")
        if (type(ts) is not int or not start_ms <= ts < end_ms
            or ts < last or not isinstance(ident, str) or not ident
            or ident in seen or record.get("instrument_name") != asset + "-PERPETUAL"):
            raise HistoryImportError("wrong trade instrument, time, order or identity")
        price, quantity = record.get("price"), record.get("amount")
        try:
            p, q = float(price), float(quantity)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HistoryImportError("missing native trade price/size") from exc
        if (not math.isfinite(p) or not math.isfinite(q)
            or p <= 0 or q <= 0):
            raise HistoryImportError("invalid native trade prices or size")
        seen.add(ident)
        last = ts
    return rows, more


async def query(client: httpx.AsyncClient, *, asset: str,
                start_ms: int, end_ms: int) -> tuple[bytes, str]:
    # Deribit's time interval end is inclusive, so use end_ms - 1.
    params = {"instrument_name": asset + "-PERPETUAL",
              "start_timestamp": start_ms, "end_timestamp": end_ms - 1,
              "sorting": "asc", "include_old": "true", "count": PAGE_LIMIT}
    async with client.stream("GET", URL, params=params,
                             follow_redirects=False) as response:
        if response.status_code != 200:
            raise HistoryImportError(
                f"Deribit historical API HTTP {response.status_code}")
        if (response.request.url.host != "history.deribit.com"
            or response.request.url.path !=
                "/api/v2/public/get_last_trades_by_instrument_and_time"):
            raise HistoryImportError("Deribit historical API host mismatch")
        raw = bytearray()
        async for piece in response.aiter_bytes():
            raw.extend(piece)
            if len(raw) > MAX_SOURCE_BYTES:
                raise HistoryImportError("Deribit source chunk exceeds size budget")
        url = str(response.request.url)
    return bytes(raw), url


def stable_partition(*, asset: str, start_ms: int, end_ms: int,
                     raw: bytes, source_url: str, rows: int) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "schema": "rival-deribit-original-time-chunk-v1",
        "venue": "deribit", "asset": asset, "instrument": asset + "-PERPETUAL",
        "family": "trades", "start_ms": start_ms, "end_ms_exclusive": end_ms,
        "native_trade_rows": rows, "source_url": source_url,
        "source_sha256": digest, "original_provider_checksum_available": False,
        "whole_calendar_month_verified": False,
        "source_chunk_complete": True, "live_model_eligible": False,
    }


async def archive_chunk(drive, *, root: Path, meta: dict, raw: bytes) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    stem = ("deribit-{asset}-trades-{start_ms}-{end_ms_exclusive}-{sha}"
            .format(asset=meta["asset"], start_ms=meta["start_ms"],
                    end_ms_exclusive=meta["end_ms_exclusive"],
                    sha=meta["source_sha256"][:16]))
    original = root / (stem + ".source.json")
    manifest = root / (stem + ".manifest.json")
    if original.exists() or manifest.exists():
        raise HistoryImportError("refusing to replace original source")
    original.write_bytes(raw)
    manifest.write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n")
    src = _artifact(original, meta["native_trade_rows"])
    receipt = _artifact(manifest, 1)
    batch = "deribit-time-" + hashlib.sha256(
        (meta["asset"] + ":" + str(meta["start_ms"]) + ":"
         + str(meta["end_ms_exclusive"]) + ":" + src.sha256).encode()
    ).hexdigest()[:24]
    source_file = await drive.ensure_history_file(
        artifact=src, batch_id=batch, mime_type="application/json")
    receipt_file = await drive.ensure_manifest_file(
        artifact=receipt, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=source_file.file_id, parent_id=drive.folders.history,
        artifact=src)
    await drive.verify_downloaded_artifact(
        file_id=receipt_file.file_id, parent_id=drive.folders.manifests,
        artifact=receipt)
    return {"start_ms": meta["start_ms"], "end_ms": meta["end_ms_exclusive"],
            "trades": meta["native_trade_rows"], "source_sha256": src.sha256,
            "readback_verified": True}


async def collect_hour(client, drive, *, asset: str,
                       start_ms: int, end_ms: int, root: Path) -> dict:
    # Stack provides bounded splitting with no silently omitted full page.
    queue = [(start_ms, end_ms)]
    results, requests, seen = [], 0, set()
    failures = []
    while queue:
        if requests >= MAX_REQUESTS:
            failures.append({"reason": "bounded_request_budget_exhausted",
                             "unverified_windows": queue[:]})
            break
        left, right = queue.pop()
        requests += 1
        await asyncio.sleep(REQUEST_SPACING)
        raw, url = await query(client, asset=asset,
                               start_ms=left, end_ms=right)
        trades, more = inspect_page(
            raw, asset=asset, start_ms=left, end_ms=right)
        if more:
            midpoint = left + (right - left) // 2
            if midpoint <= left or right - left <= MIN_SPLIT_MS:
                failures.append({"reason": "unresolved_overflow_at_minimum_window",
                                 "start_ms": left, "end_ms": right})
                continue
            # Process earlier partition before later, preserving precise windows.
            queue.extend([(midpoint, right), (left, midpoint)])
            continue
        if not trades:
            # Empty is not independent proof that the contract traded zero volume.
            failures.append({"reason": "empty_source_window_unverified",
                             "start_ms": left, "end_ms": right})
            continue
        if any(t["trade_id"] in seen for t in trades):
            raise HistoryImportError("historical trade ID overlaps a separate source chunk")
        seen.update(t["trade_id"] for t in trades)
        meta = stable_partition(asset=asset, start_ms=left,
                                end_ms=right, raw=raw,
                                source_url=url, rows=len(trades))
        results.append(await archive_chunk(drive, root=root, meta=meta, raw=raw))
    return {
        "asset": asset, "start_ms": start_ms, "end_ms": end_ms,
        "source_requests": requests, "verified_chunks": results,
        "verified_trades": sum(t["trades"] for t in results),
        "unverified_windows": failures,
        "hour_fully_accounted_for": not failures and not queue,
        "calendar_month_complete": False,
    }


async def acquire(*, day: str, hour: int, assets: tuple[str, ...],
                  root: Path = Path("data/deribit-original-hourly")) -> dict:
    start, end = hour_range(day, hour)
    if not assets or len(set(assets)) != len(assets) or any(
        a not in ASSETS for a in assets
    ):
        raise HistoryImportError("invalid historical Deribit contract scope")
    drive = _drive(Settings())
    results = []
    try:
        async with httpx.AsyncClient(timeout=50, follow_redirects=False) as client:
            for asset in assets:
                try:
                    results.append(await collect_hour(
                        client, drive, asset=asset, start_ms=start, end_ms=end,
                        root=root))
                except (HistoryImportError, httpx.HTTPError, OSError) as exc:
                    results.append({
                        "asset": asset, "hour_fully_accounted_for": False,
                        "source_blocker": str(exc)[:180], "verified_chunks": [],
                    })
                    if "HTTP 429" in str(exc) or "HTTP 403" in str(exc):
                        break
    finally:
        await drive.aclose()
    report = {"schema": "rival-deribit-hourly-source-acquisition-v1",
              "date": day, "hour": hour, "assets": results,
              "all_source_months_complete": False, "live_model_promoted": False}
    output = Path("data/native-reports/deribit-hourly-source-acquisition.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default="2021-01-02")
    parser.add_argument("--hour", type=int, default=0)
    parser.add_argument("--assets", nargs="+", choices=ASSETS,
                        default=["BTC", "ETH"])
    args = parser.parse_args()
    try:
        result = asyncio.run(acquire(day=args.day, hour=args.hour,
                                     assets=tuple(args.assets)))
    except (HistoryImportError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"Deribit hourly acquisition BLOCKED: {exc}\n")
    print(json.dumps({"date": result["date"], "hour": result["hour"],
                      "assets": [{
                          "asset": x["asset"],
                          "verified_trades": x.get("verified_trades", 0),
                          "chunks": len(x["verified_chunks"]),
                          "hour_complete": x["hour_fully_accounted_for"],
                          "blocker": x.get("source_blocker"),
                      } for x in result["assets"]],
                      "all_months_complete": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
