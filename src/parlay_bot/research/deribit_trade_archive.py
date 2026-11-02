"""A bounded original 2021 Deribit trade sample, preserved in private Drive.

Proof of old native records only: never import into trading or claim a full month.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

BASE = "https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"
MAX_BYTES = 3_000_000
MAX_COUNT = 1000


def bounds(day: str, hour: int = 0, minute: int = 0) -> tuple[int, int]:
    try:
        begin = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid date") from exc
    if (
        not (datetime(2021, 1, 1, tzinfo=UTC) <= begin < datetime.now(UTC))
        or not 0 <= hour <= 23 or not 0 <= minute <= 59
    ):
        raise HistoryImportError("date/window not within permitted completed history")
    start = begin + timedelta(hours=hour, minutes=minute)
    end = start + timedelta(minutes=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def validate(raw: bytes, *, asset: str, start: int, end: int) -> dict:
    if asset not in ("BTC", "ETH") or not 0 < len(raw) <= MAX_BYTES:
        raise HistoryImportError("invalid original sample size or asset")
    try:
        data = json.loads(raw)
        result = data["result"]
        trades = result["trades"]
    except (ValueError, TypeError, KeyError) as exc:
        raise HistoryImportError("invalid official historical trade response") from exc
    if (
        not isinstance(trades, list) or not 0 < len(trades) <= MAX_COUNT
        or result.get("has_more") is not False
    ):
        raise HistoryImportError("empty/truncated source minute; cannot publish")
    seen = set()
    last = start
    for row in trades:
        if not isinstance(row, dict):
            raise HistoryImportError("invalid source trade")
        stamp = row.get("timestamp")
        ident = row.get("trade_id")
        if (
            type(stamp) is not int or not start <= stamp < end or stamp < last
            or row.get("instrument_name") != f"{asset}-PERPETUAL"
            or not isinstance(ident, str) or not ident or ident in seen
        ):
            raise HistoryImportError("trade window/order/identity mismatch")
        seen.add(ident)
        last = stamp
    return {"asset": asset, "venue": "deribit",
            "contract": f"{asset}-PERPETUAL", "family": "trades",
            "records": len(trades), "source_sha256": hashlib.sha256(raw).hexdigest(),
            "start_ms": start, "end_ms": end,
            "calendar_month_verified": False, "historical_model_eligible": False,
            "trading_changed": False}


async def one(client, drive, *, asset: str, day: str, root: Path) -> dict:
    start, end = bounds(day)
    query = {"instrument_name": f"{asset}-PERPETUAL",
             "start_timestamp": start, "end_timestamp": end,
             "sorting": "asc", "count": MAX_COUNT, "include_old": "true"}
    async with client.stream("GET", BASE, params=query, follow_redirects=False) as response:
        if response.status_code != 200:
            raise HistoryImportError(f"Deribit historical HTTP {response.status_code}")
        if not str(response.request.url).startswith(BASE + "?"):
            raise HistoryImportError("source URL changed host/path")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HistoryImportError("source exceeded maximum safe response")
    proof = validate(bytes(raw), asset=asset, start=start, end=end)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"deribit-{asset}-PERPETUAL-{day}-0000.original.json"
    manifest_path = root / f"deribit-{asset}-PERPETUAL-{day}-0000.manifest.json"
    if path.exists() or manifest_path.exists():
        raise HistoryImportError("would overwrite original source evidence")
    path.write_bytes(raw)
    proof["source_url"] = str(response.request.url)
    proof["source_file"] = path.name
    proof["first_observed_at"] = datetime.now(UTC).isoformat()
    proof["source_checksum_provided_by_exchange"] = False
    manifest_path.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    original = _artifact(path, proof["records"])
    manifest = _artifact(manifest_path, 1)
    batch = "deribit-original-" + hashlib.sha256(
        f"{asset}:{start}:{end}".encode()).hexdigest()[:24]
    saved = await drive.ensure_history_file(
        artifact=original, batch_id=batch, mime_type="application/json")
    metadata = await drive.ensure_manifest_file(
        artifact=manifest, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history, artifact=original)
    await drive.verify_downloaded_artifact(
        file_id=metadata.file_id, parent_id=drive.folders.manifests, artifact=manifest)
    return {"asset": asset, "records": proof["records"],
            "source_sha256": original.sha256,
            "manifest_sha256": manifest.sha256,
            "drive_actual_bytes_readback_verified": True,
            "whole_2021_to_present_verified": False,
            "live_models_changed": False}


async def collect(*, day: str, assets: tuple[str, ...],
                  root: Path = Path("data/deribit-original-2021")) -> dict:
    if not assets or len(set(assets)) != len(assets) or any(
        a not in ("BTC", "ETH") for a in assets
    ):
        raise HistoryImportError("unsupported Deribit historical contract")
    drive = _drive(Settings())
    out = []
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            for asset in assets:
                out.append(await one(client, drive, asset=asset, day=day, root=root))
    finally:
        await drive.aclose()
    return {"schema": "rival-deribit-2021-original-trade-proof-v1",
            "readback_verified": len(out), "sources": out,
            "complete_2021_to_present": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default="2021-01-01")
    parser.add_argument("--assets", nargs="+", choices=("BTC", "ETH"),
                        default=["BTC", "ETH"])
    args = parser.parse_args()
    try:
        outcome = asyncio.run(collect(day=args.day, assets=tuple(args.assets)))
    except (HistoryImportError, httpx.HTTPError, OSError, RuntimeError,
            ValueError, TypeError) as exc:
        parser.exit(2, f"Deribit original source proof unverified: {exc}\n")
    path = Path("data/native-reports/deribit-2021-trade-source-proof.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outcome, sort_keys=True, indent=2) + "\n")
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
