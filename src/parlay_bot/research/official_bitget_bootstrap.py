"""Bounded official-source Bitget V3 six-month native-research bootstrap.

Fetches real USDT perpetual market candles directly from the exchange's public
history endpoint on an independent GitHub runner. Verifies exact 5m calendar
coverage, uploads original-source research artifacts into the existing private
Drive with SHA-256 readback, then trains/imports through the existing
source-native *research-only* pipeline. Never manufactures candles, makes a
historical prediction, touches Discloud PostgreSQL, or promotes a model.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from itertools import pairwise
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.batch import complete_months
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.source_native import (
    SOURCE,
    restore_verified_source_months,
)
from parlay_bot.research.source_native_offline import run_native_research

ENDPOINT = "https://api.bitget.com/api/v3/market/history-candles"
INSTRUMENTS = "https://api.bitget.com/api/v3/market/instruments"
STEP = timedelta(minutes=5)
MAX_PAGE = 100
SOURCE_INTEGRITY = "bitget-v3-public-history-v1"
ASSETS = ("BTC", "ETH", "SOL", "XRP")


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise HistoryImportError("invalid Bitget calendar month") from exc
    if start.strftime("%Y-%m") != month:
        raise HistoryImportError("noncanonical Bitget month")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _positive(value: object, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        raise HistoryImportError("Bitget numeric field has invalid type")
    try:
        val = float(value)
    except (ValueError, OverflowError) as exc:
        raise HistoryImportError("Bitget malformed price or volume") from exc
    if not math.isfinite(val) or val < 0 or (not allow_zero and val == 0):
        raise HistoryImportError("Bitget invalid OHLCV")
    return val


async def _public(client: httpx.AsyncClient, url: str, params: dict) -> bytes:
    try:
        response = await client.get(url, params=params, follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 128_000:
            raise HistoryImportError("oversized Bitget response")
        return response.content
    except httpx.HTTPError as exc:
        raise HistoryImportError("official Bitget endpoint unavailable") from exc


def _decode(payload: bytes) -> dict:
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise HistoryImportError("invalid official Bitget JSON") from exc
    if not isinstance(obj, dict) or obj.get("code") != "00000":
        raise HistoryImportError("official Bitget source did not confirm success")
    return obj


async def verify_instrument(client: httpx.AsyncClient, asset: str) -> None:
    if asset not in ASSETS:
        raise HistoryImportError("unsupported historical Bitget asset")
    obj = _decode(await _public(client, INSTRUMENTS, {
        "category": "USDT-FUTURES", "symbol": asset + "USDT",
    }))
    rows = obj.get("data")
    if not isinstance(rows, list) or sum(
        1 for row in rows if isinstance(row, dict)
        and row.get("symbol") == asset + "USDT"
        and row.get("category") == "USDT-FUTURES"
        and row.get("type") == "perpetual"
        and row.get("status") == "online"
        and row.get("baseCoin") == asset and row.get("quoteCoin") == "USDT"
    ) != 1:
        raise HistoryImportError("Bitget native perpetual instrument identity unverified")


async def download_month(
    client: httpx.AsyncClient, *, asset: str, month: str, root: Path,
    now: datetime | None = None, delay: float = 0.2,
):
    """Only persist a verified, fully contiguous *completed* calendar month."""
    if asset not in ASSETS or not 0 <= delay <= 2:
        raise HistoryImportError("invalid bounded Bitget import")
    start, end = _month_bounds(month)
    observed = now or datetime.now(UTC)
    if observed.utcoffset() is None or end > observed.astimezone(UTC):
        raise HistoryImportError("Bitget month not completed at observation time")
    stamps = []
    raw_digest = hashlib.sha256()
    cursor = start
    while cursor < end:
        limit = min(MAX_PAGE, int((end - cursor) / STEP))
        until = cursor + limit * STEP
        params = {
            "category": "USDT-FUTURES", "symbol": asset + "USDT",
            "interval": "5m", "type": "market",
            "startTime": str(int(cursor.timestamp() * 1000) - 1),
            "endTime": str(int(until.timestamp() * 1000) - 1),
            "limit": str(limit),
        }
        payload = await _public(client, ENDPOINT, params)
        raw_digest.update(cursor.isoformat().encode() + b"\n" + payload)
        rows = _decode(payload).get("data")
        if not isinstance(rows, list) or len(rows) > limit + 1:
            raise HistoryImportError("malformed or oversized Bitget candle page")
        received = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                raise HistoryImportError("malformed Bitget OHLCV row")
            raw_ts = row[0]
            if (isinstance(raw_ts, bool)
                or not isinstance(raw_ts, (int, str))
                or not str(raw_ts).isascii()
                or not str(raw_ts).isdecimal()
                or len(str(raw_ts)) != 13):
                raise HistoryImportError("Bitget candle timestamp invalid")
            milliseconds = int(raw_ts)
            if milliseconds % 300000:
                raise HistoryImportError("off-grid Bitget candle")
            opened = datetime.fromtimestamp(milliseconds / 1000, UTC)
            if opened == cursor - STEP:
                # The official endpoint can return one older boundary candle.
                continue
            if opened < cursor or opened >= until or opened in received:
                raise HistoryImportError("duplicate or wrong-month Bitget candle")
            op, high, low, close = (_positive(x) for x in row[1:5])
            volume = _positive(row[5], allow_zero=True)
            if low > min(op, close) or high < max(op, close) or high < low:
                raise HistoryImportError("invalid Bitget candle price envelope")
            received[opened] = (op, high, low, close, volume)
        for index in range(limit):
            opened = cursor + index * STEP
            if opened not in received:
                raise HistoryImportError(
                    "Bitget exact 5m month has missing source candle; publication blocked"
                )
            op, high, low, close, volume = received[opened]
            stamps.append({
                "venue": "bitget", "contract": asset + "USDT", "quote": "USDT",
                "interval_minutes": 5, "opened_at": opened.isoformat(),
                "closed_at": (opened + STEP - timedelta(milliseconds=1)).isoformat(),
                "first_observed_at": observed.astimezone(UTC).isoformat(),
                "open": op, "high": high, "low": low, "close": close,
                "volume_base": volume, "trades": -1,
            })
        cursor = until
        if cursor < end and delay:
            await asyncio.sleep(delay)
    expected = int((end - start) / STEP)
    if len(stamps) != expected:
        raise HistoryImportError("Bitget completed month count mismatch")
    # Only call _persist after EVERY exact source slot has passed validation.
    return _persist(
        source=SOURCE, key=f"bitget-{asset}USDT-USDT-5m-{month}",
        records=stamps, raw_sha=raw_digest.hexdigest(),
        url=ENDPOINT, out_dir=root,
        extra={
            "venue": "bitget", "contract": asset + "USDT", "quote": "USDT",
            "interval_minutes": 5, "checksum_verified": True,
            "source_integrity": SOURCE_INTEGRITY,
            "provider_signed_checksum": False,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "actual_fills_verified": False,
            "source_transport": "official_public_https",
        },
    )


async def bootstrap(
    *, drive, client: httpx.AsyncClient, asset: str, months: list[str],
    root: Path, report_dir: Path, now: datetime | None = None,
) -> dict:
    """Resume from exact Drive manifests, then publish new months atomically."""
    clock = now or datetime.now(UTC)
    if (clock.utcoffset() is None or asset not in ASSETS or len(months) != 6
        or months != sorted(months)):
        raise HistoryImportError("invalid Bitget six-month research scope")
    for a, b in pairwise(months):
        if _month_bounds(a)[1] != _month_bounds(b)[0]:
            raise HistoryImportError("nonconsecutive native research months")
    await verify_instrument(client, asset)
    existing = await restore_verified_source_months(
        drive, venue="bitget", months=months, root=root,
    )
    verified = set()
    for path in existing:
        meta = json.loads(Path(path).read_bytes())
        if meta.get("contract") == asset + "USDT" and meta.get("quote") == "USDT":
            from parlay_bot.research.audit import audit_partition
            partition = audit_partition(Path(path))
            if partition["full_calendar_month_verified"]:
                verified.add(partition["month"])
    added = []
    for month in months:
        if month in verified:
            continue
        result = await download_month(
            client, asset=asset, month=month, root=root, now=clock,
        )
        history = _artifact(result.data_path, result.records)
        manifest = _artifact(result.manifest_path, 1)
        batch_id = "native-" + history.sha256[:24]
        published_history = await drive.ensure_history_file(
            artifact=history, batch_id=batch_id,
            mime_type="application/x-ndjson",
        )
        await drive.verify_downloaded_artifact(
            file_id=published_history.file_id,
            parent_id=drive.folders.history, artifact=history,
        )
        published_manifest = await drive.ensure_manifest_file(
            artifact=manifest, batch_id=batch_id,
        )
        await drive.verify_downloaded_artifact(
            file_id=published_manifest.file_id,
            parent_id=drive.folders.manifests, artifact=manifest,
        )
        added.append(month)
    # This is an existing independent source-native research pipeline; it
    # publishes research candidates, not trading models or live signals.
    evaluated = await run_native_research(
        drive=drive, venue="bitget", months=months,
        root=root, report_dir=report_dir, publish=True,
    )
    return {
        "venue": "bitget", "asset": asset,
        "restored_full_months": sorted(verified),
        "newly_published_months": added,
        "status": evaluated["status"],
        "published_research_models": evaluated.get("published_models", 0),
        "live_approved": False, "trades_placed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only Bitget native six-month bootstrap")
    parser.add_argument("--asset", choices=ASSETS, default="BTC")
    parser.add_argument("--root", type=Path, default=Path("data/native-research"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args()
    async def run():
        drive = _drive(Settings())
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            try:
                return await bootstrap(
                    drive=drive, client=client, asset=args.asset,
                    months=complete_months(now=datetime.now(UTC), count=6),
                    root=args.root, report_dir=args.report_dir,
                )
            finally:
                await drive.aclose()
    try:
        result = asyncio.run(run())
    except (HistoryImportError, OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Bitget native bootstrap not yet qualified: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if (result["status"] == "verified_research_published"
                 and result["published_research_models"] > 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
