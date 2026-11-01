"""Read-only, source-native OKX historical research bootstrap.

Start with verified 5m perpetual candles, and separately inventory the more
advanced official trade, one-minute, funding, and L2 archives. An available
archive URL is NOT interpreted as having been downloaded or trained.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlparse

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.batch import complete_months
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.source_native import SOURCE, restore_verified_source_months
from parlay_bot.research.source_native_offline import run_native_research

BASE = "https://www.okx.com"
CANDLES = BASE + "/api/v5/market/history-candles"
INSTRUMENTS = BASE + "/api/v5/public/instruments"
ARCHIVES = BASE + "/api/v5/public/market-data-history"
INTEGRITY = "okx-v5-public-history-5m-v1"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
FIRST = "2023-07"
STEP = timedelta(minutes=5)
MAX_PAGES_PER_MONTH = 500
MODULES = {1: "trades", 2: "one_minute_candles", 3: "funding_rates",
           4: "orderbook_l2_400", 5: "orderbook_l2_5000"}


def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        raise HistoryImportError("invalid OKX calendar month") from exc
    if start.strftime("%Y-%m") != month or month < FIRST:
        raise HistoryImportError("OKX month outside selected historical range")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


async def official(client: httpx.AsyncClient, url: str, params: dict) -> list:
    if url not in (CANDLES, INSTRUMENTS, ARCHIVES):
        raise HistoryImportError("untrusted OKX endpoint")
    try:
        response = await client.get(url, params=params, follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 700_000:
            raise HistoryImportError("oversized OKX public JSON")
        data = response.json()
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        raise HistoryImportError("official OKX API request failed") from exc
    if (not isinstance(data, dict) or data.get("code") != "0"
        or not isinstance(data.get("data"), list)):
        raise HistoryImportError("unverified official OKX response")
    return data["data"]


async def verify_contract(client: httpx.AsyncClient, asset: str) -> str:
    if asset not in ASSETS:
        raise HistoryImportError("unsupported native OKX asset")
    contract = f"{asset}-USDT-SWAP"
    rows = await official(client, INSTRUMENTS, {"instType": "SWAP", "instId": contract})
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("instId") == contract
               and row.get("instType") == "SWAP"
               and row.get("settleCcy") == "USDT"
               and row.get("ctType") == "linear"]
    if len(matches) != 1:
        raise HistoryImportError("OKX swap contract identity unverified")
    return contract


def number(value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise HistoryImportError("non-numeric OKX OHLCV field")
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise HistoryImportError("invalid OKX price or volume") from exc
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise HistoryImportError("non-finite, negative, or zero OKX market field")
    return value


async def download_month(client: httpx.AsyncClient, *, asset: str, month: str,
                         root: Path, now: datetime | None = None, delay: float = 0.13):
    """Paginate older candles. Publish only a complete 5m UTC calendar grid."""
    if asset not in ASSETS or not 0 <= delay <= 2:
        raise HistoryImportError("invalid native OKX downloader scope")
    start, end = month_bounds(month)
    observed = now or datetime.now(UTC)
    if observed.utcoffset() is None or observed.astimezone(UTC) < end:
        raise HistoryImportError("OKX calendar month has not ended")
    begin_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    boundary = end_ms
    seen = {}
    digest = hashlib.sha256()
    for _ in range(MAX_PAGES_PER_MONTH):
        rows = await official(client, CANDLES, {
            "instId": f"{asset}-USDT-SWAP", "bar": "5m",
            "after": str(boundary), "limit": "100",
        })
        if not rows or len(rows) > 100:
            raise HistoryImportError("OKX historical page empty or oversized")
        oldest = None
        digest.update(str(boundary).encode("ascii"))
        digest.update(json.dumps(rows, separators=(",", ":")).encode("utf-8"))
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                raise HistoryImportError("malformed OKX historical candle")
            ts = row[0]
            if (not isinstance(ts, str) or len(ts) != 13
                or not ts.isascii() or not ts.isdecimal()):
                raise HistoryImportError("invalid OKX timestamp")
            millis = int(ts)
            if millis % 300_000:
                raise HistoryImportError("non-five-minute OKX candle")
            oldest = millis if oldest is None else min(oldest, millis)
            if millis >= boundary:
                raise HistoryImportError("repeated or future OKX historical page")
            if millis >= end_ms or millis < begin_ms:
                continue
            if millis in seen or str(row[8]) != "1":
                raise HistoryImportError("duplicate or unfinished OKX candle")
            op, hi, low, close = (number(v, positive=True) for v in row[1:5])
            contracts, base, quoted = (number(v) for v in row[5:8])
            if low > min(op, close) or hi < max(op, close) or hi < low:
                raise HistoryImportError("invalid OKX OHLC price envelope")
            dt = datetime.fromtimestamp(millis / 1000, UTC)
            seen[millis] = {
                "venue": "okx", "contract": f"{asset}-USDT-SWAP",
                "quote": "USDT", "interval_minutes": 5,
                "opened_at": dt.isoformat(),
                "closed_at": (dt + STEP - timedelta(milliseconds=1)).isoformat(),
                "first_observed_at": observed.astimezone(UTC).isoformat(),
                "open": op, "high": hi, "low": low, "close": close,
                "volume_base": base, "volume_contracts": contracts,
                "volume_quote": quoted, "trades": -1,
            }
        if oldest is None or oldest >= boundary:
            raise HistoryImportError("OKX pagination failed to advance")
        if oldest <= begin_ms:
            break
        boundary = oldest
        if delay:
            await asyncio.sleep(delay)
    else:
        raise HistoryImportError("OKX historical month exceeds safety page cap")
    stamps = list(range(begin_ms, end_ms, 300_000))
    if len(seen) != len(stamps) or set(seen) != set(stamps):
        raise HistoryImportError("missing exact OKX five-minute candles: month blocked")
    return _persist(
        source=SOURCE, key=f"okx-{asset}-USDT-SWAP-USDT-5m-{month}",
        records=[seen[ts] for ts in stamps],
        raw_sha=digest.hexdigest(), url=CANDLES, out_dir=root,
        extra={
            "venue": "okx", "contract": f"{asset}-USDT-SWAP",
            "quote": "USDT", "interval_minutes": 5,
            "checksum_verified": True, "source_integrity": INTEGRITY,
            "source_transport": "official_public_https",
            "provider_signed_checksum": False,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "original_observation_time_known": False,
        },
    )


async def inventory_extra_modules(client: httpx.AsyncClient, *,
                                   asset: str, month: str) -> dict:
    """Discover official 2023+ archives without claiming they were downloaded."""
    start, _ = month_bounds(month)
    evidence = {}
    for number_id, label in MODULES.items():
        # 5000-level L2 began in November 2025, so older months are absent.
        if number_id == 5 and month < "2025-11":
            evidence[label] = {"files": [], "status": "not_available_for_period"}
            continue
        offset = timezone(timedelta(hours=8)) if number_id in (1, 2, 3) else UTC
        date = datetime(start.year, start.month, 1, tzinfo=offset)
        response = await official(client, ARCHIVES, {
            "module": str(number_id), "instType": "SWAP",
            "instFamilyList": f"{asset}-USDT",
            "dateAggrType": "monthly",
            "begin": str(int(date.timestamp() * 1000)),
            "end": str(int(date.timestamp() * 1000)),
        })
        files = []
        for item in response:
            if not isinstance(item, dict) or item.get("dateAggrType") != "monthly":
                raise HistoryImportError("unexpected OKX archive catalog")
            for group in item.get("details", []):
                if (not isinstance(group, dict)
                    or group.get("instFamily") != f"{asset}-USDT"
                    or group.get("instType") != "SWAP"):
                    raise HistoryImportError("wrong OKX archived instrument")
                for record in group.get("groupDetails", []):
                    if not isinstance(record, dict):
                        raise HistoryImportError("malformed OKX archive file record")
                    url, filename = record.get("url"), record.get("filename")
                    if not isinstance(url, str) or not isinstance(filename, str):
                        raise HistoryImportError("missing archived OKX file identity")
                    parsed = urlparse(url)
                    if (parsed.scheme != "https" or parsed.hostname != "static.okx.com"
                        or parsed.query or parsed.fragment or parsed.username
                        or not filename.startswith(f"{asset}-USDT-SWAP-")
                        or not filename.endswith((".zip", ".tar.gz"))
                        or not parsed.path.endswith("/" + filename)):
                        raise HistoryImportError("unverified OKX archive host/filename")
                    files.append({"filename": filename, "source_url": url})
        evidence[label] = {"files": files,
                           "status": "cataloged_not_downloaded" if files else "unavailable"}
    return {"venue": "okx", "asset": asset, "month": month,
            "modules": evidence, "all_modules_imported": False}


async def bootstrap(*, drive, client: httpx.AsyncClient, asset: str, root: Path,
                    report_dir: Path, max_new_months: int = 3,
                    now: datetime | None = None,
                    download_delay: float = 0.13) -> dict:
    """Qualify latest 6 months first, then extend historic catalog back to 2023."""
    clock = now or datetime.now(UTC)
    if (asset not in ASSETS or not 1 <= max_new_months <= 6
        or not 0 <= download_delay <= 2):
        raise HistoryImportError("invalid bounded OKX research batch")
    recent = complete_months(now=clock, count=6)
    if any(month_bounds(a)[1] != month_bounds(b)[0]
           for a, b in pairwise(recent)):
        raise HistoryImportError("native OKX training months not contiguous")
    await verify_contract(client, asset)

    # The first two runs target the latest six complete months so native
    # challengers can be trained promptly. Then work systematically from
    # July 2023. No existing monthly snapshots are replaced or renamed.
    older = []
    current = month_bounds(FIRST)[0]
    newest_old = month_bounds(recent[0])[0]
    while current < newest_old:
        older.append(current.strftime("%Y-%m"))
        current = month_bounds(current.strftime("%Y-%m"))[1]
    blocks = [recent] + [older[i:i + 6] for i in range(0, len(older), 6)]
    from parlay_bot.research.audit import audit_catalog

    for block in blocks:
        await restore_verified_source_months(
            drive, venue="okx", months=block, root=root,
            contract=f"{asset}-USDT-SWAP",
        )
        inventory = audit_catalog(root, months=block, sources=(SOURCE,))
        verified = {
            row["month"] for row in inventory["partitions"]
            if row["venue"] == "okx"
            and row["contract"] == f"{asset}-USDT-SWAP"
            and row["quote"] == "USDT" and row["full_calendar_month_verified"]
        }
        missing = [month for month in block if month not in verified]
        if not missing:
            continue
        # Verify latest coverage first, then proceed forward from July 2023.
        targets = missing[-max_new_months:] if block == recent else missing[:max_new_months]
        added = []
        for month in targets:
            result = await download_month(
                client, asset=asset, month=month, root=root, now=clock,
                delay=download_delay,
            )
            history = _artifact(result.data_path, result.records)
            manifest = _artifact(result.manifest_path, 1)
            key = "native-" + history.sha256[:24]
            saved = await drive.ensure_history_file(
                artifact=history, batch_id=key, mime_type="application/x-ndjson",
            )
            await drive.verify_downloaded_artifact(
                file_id=saved.file_id, parent_id=drive.folders.history,
                artifact=history,
            )
            meta = await drive.ensure_manifest_file(
                artifact=manifest, batch_id=key,
            )
            await drive.verify_downloaded_artifact(
                file_id=meta.file_id, parent_id=drive.folders.manifests,
                artifact=manifest,
            )
            added.append(month)

        # A six-month segment can only produce models after all its source
        # months pass the exact zero-gap check. A shorter transition segment
        # is archived but is NEVER falsely treated as six complete months.
        if len(block) == 6:
            receipt = await run_native_research(
                drive=drive, venue="okx", months=block, root=root,
                report_dir=report_dir, publish=True,
            )
        else:
            receipt = {"status": "under_six_months",
                       "published_models": 0}
        report_dir.mkdir(parents=True, exist_ok=True)
        # The advanced modules are cataloged, not silently marked imported.
        for month in targets:
            catalog = await inventory_extra_modules(
                client, asset=asset, month=month,
            )
            (report_dir / f"okx-extra-inventory-{asset}-{month}.json").write_text(
                json.dumps(catalog, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        return {
            "venue": "okx", "asset": asset,
            "newly_published_months": added,
            "target_scope_months": block,
            "native_status": receipt["status"],
            "native_models_published": receipt["published_models"],
            "full_2023_to_present_verified": False,
            "historical_auxiliary_modules_qualified": False,
            "live_approved": False, "trades_placed": False,
        }
    return {
        "venue": "okx", "asset": asset,
        "newly_published_months": [],
        "native_status": "all_candle_months_already_verified",
        "native_models_published": 0,
        "full_2023_to_present_verified": True,
        "historical_auxiliary_modules_qualified": False,
        "live_approved": False, "trades_placed": False,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="OKX source-native research only")
    parser.add_argument("--asset", choices=ASSETS, default="BTC")
    args = parser.parse_args()
    async def runner():
        drive = _drive(Settings())
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            try:
                return await bootstrap(
                    drive=drive, client=client, asset=args.asset,
                    root=Path("data/native-research"),
                    report_dir=Path("data/native-reports"),
                )
            finally:
                await drive.aclose()
    try:
        result = asyncio.run(runner())
    except (HistoryImportError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"OKX source-native import unverified: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if (result["native_status"] == "verified_research_published"
                 and result["native_models_published"] > 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
