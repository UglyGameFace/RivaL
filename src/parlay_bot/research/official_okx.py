"""Read-only, fail-closed OKX swap 5m history research (not live predictions)."""
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
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.batch import complete_months
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.source_native import SOURCE, restore_verified_source_months

API = "https://www.okx.com/api/v5"
CANDLES = API + "/market/history-candles"
INSTRUMENTS = API + "/public/instruments"
INTEGRITY = "official-okx-rest-observed-at-v1"
STEP = timedelta(minutes=5)
ASSETS = frozenset(("BTC", "ETH", "SOL", "XRP"))


def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid OKX calendar month") from exc
    if start.strftime("%Y-%m") != month or month < "2023-07":
        raise HistoryImportError("OKX public archive starts July 2023")
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end


async def okx_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    try:
        response = await client.get(url, params=params, follow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200 or len(response.content) > 120_000:
            raise HistoryImportError("invalid/oversized OKX response")
        obj = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HistoryImportError("OKX public API unavailable") from exc
    if (not isinstance(obj, dict) or obj.get("code") != "0"
        or not isinstance(obj.get("data"), list)):
        raise HistoryImportError("unverified official OKX result")
    return obj


async def instrument_multiplier(client: httpx.AsyncClient, asset: str) -> float:
    if asset not in ASSETS:
        raise HistoryImportError("unsupported OKX research asset")
    name = f"{asset}-USDT-SWAP"
    obj = await okx_get(client, INSTRUMENTS, {"instType": "SWAP", "instId": name})
    data = obj["data"]
    if len(data) != 1 or not isinstance(data[0], dict):
        raise HistoryImportError("OKX instrument not verified")
    record = data[0]
    if (record.get("instId") != name or record.get("instType") != "SWAP"
        or record.get("ctType") != "linear" or record.get("state") != "live"
        or record.get("settleCcy") != "USDT"
        or record.get("ctValCcy") != asset):
        raise HistoryImportError("wrong OKX contract or settlement")
    try:
        factor = float(record["ctVal"]) * float(record["ctMult"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise HistoryImportError("invalid OKX contract multiplier") from exc
    if not math.isfinite(factor) or factor <= 0:
        raise HistoryImportError("invalid OKX contract multiplier")
    return factor


def parse_page(payload: dict, *, asset: str, start: datetime, end: datetime,
               cursor: int, factor: float, observed: datetime) -> tuple[list[dict], int]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if (asset not in ASSETS or not isinstance(rows, list) or len(rows) > 100
        or payload.get("code") != "0" or observed.utcoffset() is None):
        raise HistoryImportError("invalid native OKX history page")
    seen = set()
    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9 or row[8] != "1":
            raise HistoryImportError("unconfirmed OKX historical candle")
        token = row[0]
        if (not isinstance(token, (int, str)) or isinstance(token, bool)
            or not str(token).isascii() or not str(token).isdecimal()
            or len(str(token)) != 13):
            raise HistoryImportError("invalid historical timestamp")
        ms = int(token)
        if ms % 300_000 or ms >= cursor or ms in seen:
            raise HistoryImportError("off-grid, duplicated or future OKX candle")
        seen.add(ms)
        opened = datetime.fromtimestamp(ms / 1000, UTC)
        if opened >= end:
            raise HistoryImportError("wrong-window OKX candle")
        if opened < start:
            continue
        try:
            op, high, low, close = map(float, row[1:5])
            contracts, quote_volume = float(row[5]), float(row[7])
        except (TypeError, ValueError, OverflowError) as exc:
            raise HistoryImportError("invalid OHLCV values") from exc
        if (not all(math.isfinite(v) for v in
                    (op, high, low, close, contracts, quote_volume))
            or min(op, high, low, close) <= 0
            or low > min(op, close) or high < max(op, close)
            or contracts < 0 or quote_volume < 0):
            raise HistoryImportError("invalid OKX OHLCV envelope")
        closed = opened + STEP - timedelta(milliseconds=1)
        if observed <= closed:
            raise HistoryImportError("historical observation predates candle close")
        records.append({
            "venue": "okx", "contract": f"{asset}-USDT-SWAP",
            "quote": "USDT", "interval_minutes": 5,
            "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
            "first_observed_at": observed.isoformat(),
            "open": op, "high": high, "low": low, "close": close,
            "volume_base": contracts * factor, "trades": -1,
        })
    return records, min(seen) if seen else cursor


async def fetch_month(client: httpx.AsyncClient, *, asset: str, month: str,
                      root: Path, factor: float, now: datetime | None = None):
    start, end = month_bounds(month)
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None or clock.astimezone(UTC) < end:
        raise HistoryImportError("source calendar month still open")
    cursor = int(end.timestamp() * 1000)
    floor = int(start.timestamp() * 1000)
    rows = {}
    digest = hashlib.sha256()
    while cursor > floor:
        params = {"instId": f"{asset}-USDT-SWAP", "bar": "5m",
                  "after": str(cursor), "limit": "100"}
        payload = await okx_get(client, CANDLES, params)
        records, oldest = parse_page(
            payload, asset=asset, start=start, end=end, cursor=cursor,
            factor=factor, observed=datetime.now(UTC),
        )
        if oldest >= cursor or not payload["data"]:
            raise HistoryImportError("OKX native page ended before month was filled")
        for row in records:
            name = row["opened_at"]
            if name in rows:
                raise HistoryImportError("overlapping OKX pages")
            rows[name] = row
        digest.update(json.dumps(payload, sort_keys=True,
                                 separators=(",", ":")).encode())
        cursor = oldest
        await asyncio.sleep(0.15)
    expected = int((end - start) / STEP)
    if len(rows) != expected:
        raise HistoryImportError(
            f"missing {expected - len(rows)} source-native OKX 5m bars"
        )
    ordered = [rows[(start + index * STEP).isoformat()]
               for index in range(expected)]
    result = _persist(
        source=SOURCE, key=f"okx-{asset}-USDT-SWAP-USDT-5m-{month}",
        records=ordered, raw_sha=digest.hexdigest(), url=CANDLES,
        out_dir=root, extra={
            "venue": "okx", "contract": f"{asset}-USDT-SWAP",
            "quote": "USDT", "interval_minutes": 5,
            "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_integrity": INTEGRITY, "source_api_bar": "5m",
            "source_api_market_type": "SWAP",
            "historical_first_observed_at_is_fetch_time": True,
            "provider_signed_checksum": False, "actual_fills_verified": False,
        },
    )
    quality = audit_partition(result.manifest_path)
    if not quality["full_calendar_month_verified"] or quality["rows"] != expected:
        raise HistoryImportError("OKX exact month audit failed")
    return result


async def bootstrap(drive, *, asset: str, months: list[str], root: Path,
                    client: httpx.AsyncClient) -> dict:
    if not 1 <= len(months) <= 6 or months != sorted(months):
        raise HistoryImportError("unbounded OKX archive batch")
    factor = await instrument_multiplier(client, asset)
    previous = await restore_verified_source_months(
        drive, venue="okx", months=months, root=root / "previous",
    )
    complete = set()
    for manifest in previous:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        if (meta.get("contract") == f"{asset}-USDT-SWAP"
            and audit_partition(manifest)["full_calendar_month_verified"]):
            complete.add(meta["file"].split("-5m-")[1][:7])
    from parlay_bot.research.publish import _artifact
    created = []
    for month in months:
        if month in complete:
            continue
        dataset = await fetch_month(
            client, asset=asset, month=month, root=root, factor=factor,
        )
        history = _artifact(dataset.data_path, dataset.records)
        manifest = _artifact(dataset.manifest_path, 1)
        batch = "native-" + history.sha256[:24]
        uploaded_history = await drive.ensure_history_file(
            artifact=history, batch_id=batch, mime_type="application/x-ndjson",
        )
        await drive.verify_downloaded_artifact(
            file_id=uploaded_history.file_id, parent_id=drive.folders.history,
            artifact=history,
        )
        uploaded_manifest = await drive.ensure_manifest_file(
            artifact=manifest, batch_id=batch,
        )
        await drive.verify_downloaded_artifact(
            file_id=uploaded_manifest.file_id,
            parent_id=drive.folders.manifests, artifact=manifest,
        )
        created.append(month)
    training = None
    if len(months) == 6:
        from parlay_bot.research.source_native_offline import run_native_research
        training = await run_native_research(
            drive=drive, venue="okx", months=months, root=root / "restored",
            report_dir=root / "reports", publish=True,
        )
        if training["status"] != "verified_research_published":
            raise HistoryImportError("restored OKX research data failed qualification")
    return {"venue": "okx", "asset": asset, "source_months": months,
            "previously_verified": sorted(complete), "new_months": created,
            "training": training, "live_model_promoted": False,
            "trades_placed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Official OKX native swap archive")
    parser.add_argument("--asset", choices=sorted(ASSETS), default="BTC")
    parser.add_argument("--from-month", default=None,
                        help="Optional six-month block from July 2023 onward")
    parser.add_argument("--root", type=Path, default=Path("data/okx-bootstrap"))
    args = parser.parse_args()
    if args.from_month:
        first, _ = month_bounds(args.from_month)
        months = []
        for offset in range(6):
            month = (first.year * 12 + first.month - 1) + offset
            year, zero_month = divmod(month, 12)
            name = f"{year:04d}-{zero_month + 1:02d}"
            _, end = month_bounds(name)
            if end > datetime.now(UTC):
                break
            months.append(name)
    else:
        months = complete_months(now=datetime.now(UTC), count=6)
    if not months:
        parser.error("no closed OKX research months available")
    from parlay_bot.research.publish import _drive
    drive = _drive(Settings())
    async def run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
            try:
                return await bootstrap(
                    drive, asset=args.asset, months=months,
                    root=args.root, client=client,
                )
            finally:
                await drive.aclose()
    try:
        output = asyncio.run(run())
    except (HistoryImportError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Official OKX bootstrap blocked: {exc}\n")
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
