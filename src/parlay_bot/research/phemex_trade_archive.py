"""Original Phemex historical public trade proof, separate from live perps.

The 2021 BTCUSD/ETHUSD contracts are USD inverse instruments, not USDT
perpetuals. Store only authentic source responses and their provenance.
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

URL = "https://api.phemex.com/exchange/public/nomics/trades"
MARKETS = ("BTCUSD", "ETHUSD")
MAX_BYTES = 3_000_000
MAX_ROWS = 10_000


def minute_bounds(day: str) -> tuple[int, int]:
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid Phemex historical UTC day") from exc
    if not datetime(2021, 1, 1, tzinfo=UTC) <= start < datetime.now(UTC):
        raise HistoryImportError("invalid Phemex historical request date")
    return int(start.timestamp() * 1000), int(
        (start + timedelta(minutes=1)).timestamp() * 1000)


def validate(raw: bytes, *, market: str, start: int, end: int) -> dict:
    if market not in MARKETS or not 0 < len(raw) <= MAX_BYTES:
        raise HistoryImportError("invalid source scope or byte limit")
    try:
        payload = json.loads(raw)
        rows = payload["data"]
    except (ValueError, TypeError, KeyError) as exc:
        raise HistoryImportError("Phemex historical trades schema invalid") from exc
    if (not isinstance(payload, dict) or payload.get("code") != 0
        or not isinstance(rows, list) or len(rows) > MAX_ROWS):
        raise HistoryImportError("Phemex historical trade source rejected")
    ids = set()
    records = []
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryImportError("invalid original source trade")
        ident = row.get("id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise HistoryImportError("missing or duplicate original trade identifier")
        ids.add(ident)
        stamp = row.get("timestamp")
        if not isinstance(stamp, str):
            raise HistoryImportError("invalid Phemex source timestamp")
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise HistoryImportError("invalid source timestamp ISO format") from exc
        if at.utcoffset() != timedelta(0):
            raise HistoryImportError("source timestamp must be UTC")
        millis = int(at.timestamp() * 1000)
        if not start <= millis < end:
            raise HistoryImportError("off-window source trade")
        if row.get("side") not in ("buy", "sell"):
            raise HistoryImportError("unverified source trade direction")
        for field in ("price", "amount_quote"):
            try:
                number = float(row[field])
            except (TypeError, ValueError, KeyError, OverflowError) as exc:
                raise HistoryImportError("missing/invalid source price or quantity") from exc
            if not math.isfinite(number) or number <= 0:
                raise HistoryImportError("invalid Phemex trade numeric value")
        records.append((millis, ident))
    if records != sorted(records):
        # Source API does not promise ascending rows. We do NOT infer missing
        # records from order; retain all raw bytes and mark source unqualified.
        ordering = "source_unsorted"
    else:
        ordering = "ascending"
    return {
        "schema_version": "rival-phemex-historical-trade-minute-v1",
        "venue": "phemex", "market": market, "contract_quote": "USD",
        "contract_kind": "inverse", "family": "trades",
        "start_timestamp_ms": start, "end_timestamp_ms": end,
        "source_rows": len(rows), "source_order": ordering,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "calendar_month_verified": False,
        "single_minute_page_complete": False,
        "historical_model_eligible": False, "live_forecasts_changed": False,
    }


async def one(client, drive, *, market: str, day: str, root: Path) -> dict:
    start, end = minute_bounds(day)
    params = {"market": market, "since": "0-0-0",
              "start": start, "end": end}
    async with client.stream("GET", URL, params=params, follow_redirects=False) as rsp:
        if rsp.status_code != 200:
            raise HistoryImportError(f"Phemex public trades HTTP {rsp.status_code}")
        if (rsp.request.url.host != "api.phemex.com"
            or rsp.request.url.path != "/exchange/public/nomics/trades"):
            raise HistoryImportError("Phemex response changed native source")
        raw = bytearray()
        async for chunk in rsp.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HistoryImportError("Phemex historical trade source too large")
        source_url = str(rsp.request.url)
    meta = validate(bytes(raw), market=market, start=start, end=end)
    if not meta["source_rows"]:
        return {"market": market, "status": "no_original_trades_returned",
                "readback_verified": False}
    root.mkdir(parents=True, exist_ok=True)
    stem = f"phemex-{market}-{day}-0000-{meta['raw_sha256'][:16]}"
    source = root / (stem + ".source.json")
    manifest = root / (stem + ".manifest.json")
    if source.exists() or manifest.exists():
        raise HistoryImportError("Phemex source bytes already exist locally")
    source.write_bytes(raw)
    meta["source_url"] = source_url
    meta["source_filename"] = source.name
    manifest.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    source_artifact = _artifact(source, meta["source_rows"])
    manifest_artifact = _artifact(manifest, 1)
    batch = "phemex-raw-v1-" + hashlib.sha256(
        f"{market}:{start}:{end}:{meta['raw_sha256']}".encode()
    ).hexdigest()[:24]
    historic = await drive.ensure_history_file(
        artifact=source_artifact, batch_id=batch, mime_type="application/json")
    receipt = await drive.ensure_manifest_file(
        artifact=manifest_artifact, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=historic.file_id, parent_id=drive.folders.history,
        artifact=source_artifact)
    await drive.verify_downloaded_artifact(
        file_id=receipt.file_id, parent_id=drive.folders.manifests,
        artifact=manifest_artifact)
    return {"market": market, "status": "native_source_readback_verified",
            "records": meta["source_rows"], "sha256": meta["raw_sha256"],
            "readback_verified": True, "calendar_month_verified": False}


async def collect(*, day: str, markets: tuple[str, ...],
                  root: Path = Path("data/phemex-2021-source-proof")) -> dict:
    minute_bounds(day)
    if not markets or len(set(markets)) != len(markets) or any(
        m not in MARKETS for m in markets
    ):
        raise HistoryImportError("untrusted Phemex source selection")
    drive = _drive(Settings())
    results = []
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            for market in markets:
                results.append(await one(
                    client, drive, market=market, day=day, root=root))
                await asyncio.sleep(0.3)
    finally:
        await drive.aclose()
    report = {
        "schema_version": "rival-phemex-2021-trade-proof-v1",
        "date": day, "venues": results,
        "readback_verified_sources": sum(
            x["readback_verified"] is True for x in results),
        "complete_calendar_months": 0, "live_models_changed": False,
        "old_usdt_perpetuals_inferred": False,
    }
    destination = Path("data/native-reports/phemex-2021-trade-proof.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default="2021-01-01")
    parser.add_argument("--markets", nargs="+", choices=MARKETS,
                        default=["BTCUSD", "ETHUSD"])
    args = parser.parse_args()
    try:
        result = asyncio.run(collect(day=args.day, markets=tuple(args.markets)))
    except (HistoryImportError, httpx.HTTPError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Phemex 2021 native trades BLOCKED: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
