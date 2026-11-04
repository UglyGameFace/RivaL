"""Fill and tidy the private Drive warehouse (GitHub Actions only).

  --dedupe   move duplicate copies to Drive's trash (kept 30 days, restorable):
             a 'rival-src__' file is a duplicate only when an original with
             the SAME MD5 already exists under the collector's own name.
  --fill     add official history the models can learn from:
             Binance order-book depth (daily bookDepth, 2023+), Binance
             open interest / positioning (daily metrics, 2021-12+) and the
             Deribit volatility index (DVOL, 1-minute, BTC and ETH, 2021-03+).
Bounded per run; safe to repeat (everything is idempotent).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import feature_signal_experiment as source

from parlay_bot.config import Settings
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.training_warehouse import OFFICIAL, PREFIX, TrainingWarehouse

ASSETS = ("BTC", "ETH", "SOL", "XRP")
BOOK_DEPTH_FROM = date(2023, 1, 1)
METRICS_FROM = date(2021, 12, 1)
DVOL_FROM = date(2021, 3, 1)
DERIBIT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"


def days(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def official_urls(today: date) -> list[str]:
    last = today - timedelta(days=2)
    urls = []
    for asset in ASSETS:
        sym = f"{asset}USDT"
        urls += [f"{OFFICIAL}daily/bookDepth/{sym}/{sym}-bookDepth-{d.isoformat()}.zip"
                 for d in days(BOOK_DEPTH_FROM, last)]
        urls += [f"{OFFICIAL}daily/metrics/{sym}/{sym}-metrics-{d.isoformat()}.zip"
                 for d in days(METRICS_FROM, last)]
    return urls


async def dedupe(warehouse: TrainingWarehouse, drive) -> dict:
    by_md5: dict[str, list[str]] = {}
    for name, (_file_id, md5, _size) in warehouse.index.items():
        by_md5.setdefault(md5, []).append(name)
    trashed = kept = 0
    for names in by_md5.values():
        originals = [n for n in names if not n.startswith(PREFIX)]
        copies = [n for n in names if n.startswith(PREFIX)]
        if not originals:
            kept += len(copies)
            continue
        for name in copies:
            file_id = warehouse.index[name][0]
            response = await drive._client.patch(
                f"{drive.DRIVE_URL}/files/{file_id}", json={"trashed": True},
                headers=await drive._headers())
            response.raise_for_status()
            trashed += 1
    return {"duplicates_trashed": trashed, "unique_copies_kept": kept}


async def fill_dvol(client, drive, index: dict, today: date, budget: int) -> dict:
    saved = 0
    for currency in ("BTC", "ETH"):
        month = DVOL_FROM
        while month < today.replace(day=1) and saved < budget:
            name = f"{PREFIX}deribit__dvol__{currency}__{month:%Y-%m}.json"
            nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            if name not in index:
                start = int(datetime.combine(month, datetime.min.time(), UTC).timestamp() * 1000)
                end = int(datetime.combine(nxt, datetime.min.time(), UTC).timestamp() * 1000) - 1
                points, cursor = [], end
                for _ in range(80):  # 1-minute points, 1,000 per page
                    response = await client.get(DERIBIT, params={
                        "currency": currency, "resolution": "60",
                        "start_timestamp": start, "end_timestamp": cursor})
                    response.raise_for_status()
                    result = response.json()["result"]
                    points += result.get("data") or []
                    cursor = result.get("continuation")
                    if not cursor:
                        break
                points = sorted({p[0]: p for p in points}.values())
                path = Path("dvol") / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({
                    "source": DERIBIT, "currency": currency, "month": f"{month:%Y-%m}",
                    "columns": ["timestamp_ms", "open", "high", "low", "close"],
                    "data": points}))
                await drive.ensure_history_file(
                    artifact=_artifact(path, len(points)),
                    batch_id="src-" + hashlib.sha256(name.encode()).hexdigest()[:40],
                    mime_type="application/json")
                path.unlink()
                saved += 1
            month = nxt
    return {"dvol_months_saved": saved}


async def main_async(args) -> dict:
    import httpx

    drive = _drive(Settings())
    try:
        cache = Path("warehouse-cache")
        cache.mkdir(exist_ok=True)
        warehouse = TrainingWarehouse(drive, cache, max_uploads=args.max_uploads)
        await warehouse.load_index()
        report: dict = {"indexed_files": len(warehouse.index)}
        if args.dedupe:
            report |= await dedupe(warehouse, drive)
        if args.fill:
            fetch = warehouse.fetcher(source.fetch)
            sem = asyncio.Semaphore(12)
            todo = [u for u in official_urls(datetime.now(UTC).date())
                    if warehouse._lookup(u) is None]
            report["official_files_missing"] = len(todo)
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                batch = todo[: args.max_uploads]
                await asyncio.gather(*(fetch(client, u, cache, sem) for u in batch))
                report |= await fill_dvol(client, drive, warehouse.index,
                                          datetime.now(UTC).date(), budget=40)
            report |= warehouse.stats
        return report
    finally:
        await drive.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dedupe", action="store_true")
    parser.add_argument("--fill", action="store_true")
    parser.add_argument("--max-uploads", type=int, default=1500)
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    print("WAREHOUSE_REPORT=" + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
