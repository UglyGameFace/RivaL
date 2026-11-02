"""Bounded capture of genuinely older Bitget Futures Tardis *sample* archives.

Tardis explicitly offers first-of-month day CSV samples without an API key
from Nov 2024 onwards. This is vendor-normalized WebSocket history, NOT the
Bitget official portal or full 2021-now market coverage. Keep original gzip
bytes immutable in private Drive with SHA-256 readback. Never use for live
recommendations or assume continuous L2 reconstruction without snapshots.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.bitget_v3_archive import ASSETS
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

SOURCE = "https://datasets.tardis.dev"
SCHEMA = "rival-bitget-tardis-public-monthly-day-v1"
FAMILIES = ("trades", "incremental_book_L2")
MAX_COMPRESSED_BYTES = 160 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 1600 * 1024 * 1024
MAX_CSV_ROWS = 25_000_000
REQUIRED = {
    "trades": {
        "exchange", "symbol", "timestamp", "local_timestamp",
        "id", "side", "price", "amount",
    },
    "incremental_book_L2": {
        "exchange", "symbol", "timestamp", "local_timestamp",
        "is_snapshot", "side", "price", "amount",
    },
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_selection(*, asset: str, month: str, family: str, now: datetime) -> str:
    if asset not in ASSETS or family not in FAMILIES:
        raise HistoryImportError("unsupported original Bitget futures symbol/family")
    if len(month) != 7 or month[4] != "-":
        raise HistoryImportError("invalid Tardis source month")
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid Tardis source month") from exc
    if (start.strftime("%Y-%m") != month or start < datetime(2024, 12, 1, tzinfo=UTC)
        or now.utcoffset() is None or start >= now.astimezone(UTC)):
        raise HistoryImportError("Tardis public day not in available Bitget history")
    return (f"{SOURCE}/v1/bitget-futures/{family}/"
            f"{start.year:04d}/{start.month:02d}/01/{asset}USDT.csv.gz")


def _positive(value: str, *, allow_zero: bool = False) -> None:
    try:
        num = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HistoryImportError("invalid Bitget futures CSV number") from exc
    if not num.is_finite() or num < 0 or (num == 0 and not allow_zero):
        raise HistoryImportError("negative, zero or nonfinite Bitget CSV number")


def verify_gzip_csv(path: Path, *, asset: str, month: str, family: str,
                    now: datetime | None = None) -> dict:
    """Fully decompress, authenticate venue/symbol/schema, and bound all rows."""
    validate_selection(
        asset=asset, month=month, family=family,
        now=now or datetime.now(UTC),
    )
    if not 0 < path.stat().st_size <= MAX_COMPRESSED_BYTES:
        raise HistoryImportError("Tardis downloaded gzip outside file-size budget")
    year, mo = (int(x) for x in month.split("-"))
    start = int(datetime(year, mo, 1, tzinfo=UTC).timestamp() * 1_000_000)
    end = start + 86_400_000_000
    count = 0
    first_ts = None
    last_ts = None
    seen_snapshot = False
    decoded_bytes = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if (not reader.fieldnames or len(reader.fieldnames) > 100
                or len(reader.fieldnames) != len(set(reader.fieldnames))
                or not REQUIRED[family].issubset(set(reader.fieldnames))):
                raise HistoryImportError("Tardis CSV source schema incompatible")
            for row in reader:
                count += 1
                if count > MAX_CSV_ROWS:
                    raise HistoryImportError("Tardis CSV row budget exceeded")
                decoded_bytes += sum(len(v or "") for v in row.values())
                if decoded_bytes > MAX_DECOMPRESSED_BYTES:
                    raise HistoryImportError("Tardis CSV decoded size budget exceeded")
                if (row["exchange"] != "bitget-futures"
                    or row["symbol"] != asset + "USDT"):
                    raise HistoryImportError("Tardis CSV venue or contract mismatch")
                try:
                    local = int(row["local_timestamp"])
                    ts = int(row["timestamp"])
                except (ValueError, TypeError) as exc:
                    raise HistoryImportError("Tardis CSV timestamp invalid") from exc
                # Tardis partitions by *arrival* timestamp; exchange clocks
                # can differ, so validate their existence but not UTC day.
                if not start <= local < end or ts <= 0:
                    raise HistoryImportError("Tardis CSV row outside original UTC day")
                first_ts = min(local, first_ts) if first_ts is not None else local
                last_ts = max(local, last_ts) if last_ts is not None else local
                _positive(row["price"])
                _positive(row["amount"], allow_zero=family == "incremental_book_L2")
                if family == "trades":
                    if row["side"] not in ("buy", "sell", "unknown"):
                        raise HistoryImportError("Tardis public trade side invalid")
                else:
                    if row["side"] not in ("bid", "ask"):
                        raise HistoryImportError("Tardis orderbook side invalid")
                    if row["is_snapshot"] not in ("true", "false"):
                        raise HistoryImportError("Tardis snapshot flag invalid")
                    seen_snapshot |= row["is_snapshot"] == "true"
    except (EOFError, OSError, UnicodeDecodeError, csv.Error) as exc:
        raise HistoryImportError("Tardis gzip CSV truncated or invalid") from exc
    if count == 0:
        raise HistoryImportError("Tardis source day contains no rows")
    return {
        "source_rows": count,
        "first_arrival_timestamp_us": first_ts,
        "last_arrival_timestamp_us": last_ts,
        "has_snapshot": seen_snapshot if family == "incremental_book_L2" else None,
        "full_day_trade_tape_verified": False,
        "full_day_orderbook_continuity_verified": False,
        "full_historical_range_verified": False,
    }


async def obtain(client: httpx.AsyncClient, *, asset: str, month: str,
                 family: str, root: Path, now: datetime) -> tuple[Path, str]:
    url = validate_selection(asset=asset, month=month, family=family, now=now)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as tmp:
        location = Path(tmp.name)
    try:
        for attempt in range(3):
            try:
                async with client.stream("GET", url, follow_redirects=False) as response:
                    if response.status_code == 200:
                        # File gzip is an actual .csv.gz payload, not the HTTP
                        # Content-Encoding layer. Reject encoded HTTP bodies,
                        # which would otherwise silently change original bytes.
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise HistoryImportError("Tardis HTTP transfer encoding is not identity")
                        size = 0
                        # Preserve bytes without blocking the async HTTP loop.
                        out = await asyncio.to_thread(location.open, "wb")
                        try:
                            async for data in response.aiter_bytes(1024 * 1024):
                                size += len(data)
                                if size > MAX_COMPRESSED_BYTES:
                                    raise HistoryImportError(
                                        "Tardis source exceeds archive byte budget"
                                    )
                                await asyncio.to_thread(out.write, data)
                        finally:
                            await asyncio.to_thread(out.close)
                        if size == 0:
                            raise HistoryImportError("Tardis source file empty")
                        return location, url
                    if response.status_code in (401, 403, 404, 451):
                        raise HistoryImportError(
                            f"Tardis source day unavailable HTTP {response.status_code}"
                        )
                    if response.status_code not in (429, 500, 502, 503, 504):
                        raise HistoryImportError(
                            f"Tardis source rejected HTTP {response.status_code}"
                        )
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise HistoryImportError("Tardis archive source unavailable") from exc
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        raise HistoryImportError("Tardis source retries exhausted")
    except BaseException:
        location.unlink(missing_ok=True)
        raise


async def publish(drive, *, source: Path, asset: str, month: str,
                  family: str, url: str, metrics: dict, root: Path) -> dict:
    if not source.is_file() or not metrics.get("source_rows"):
        raise HistoryImportError("Tardis source proof absent")
    digest = sha(source.read_bytes())
    prefix = f"bitget-tardis-{asset}-{family}-{month}-01--{digest[:16]}"
    dest = root / (prefix + ".csv.gz")
    source.replace(dest)
    meta = root / (prefix + ".manifest.json")
    proof = {
        "schema_version": SCHEMA, "original_file": dest.name,
        "original_sha256": digest,
        "source_url": url, "source_vendor": "tardis.dev",
        "source_exchange": "bitget-futures",
        "exchange": "bitget", "symbol": asset + "USDT",
        "asset": asset, "family": family, "utc_day": month + "-01",
        "source_recording": "tardis_normalized_bitget_websocket",
        "source_signed_checksum": False,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "monthly_sample_day_only": True,
        "historical_2021_coverage_verified": False,
        "entire_month_coverage_verified": False,
        "live_trading_authorized": False,
        "model_training_eligible": False,
        "prospective_forecast": False,
        **metrics,
    }
    meta.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
    data_artifact = _artifact(dest, metrics["source_rows"])
    proof_artifact = _artifact(meta, 1)
    batch = "bitget-tardis-" + hashlib.sha256(
        f"{asset}:{family}:{month}:{digest}".encode()
    ).hexdigest()[:24]
    saved = await drive.ensure_history_file(
        artifact=data_artifact, batch_id=batch, mime_type="application/gzip",
    )
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history,
        artifact=data_artifact,
    )
    stamped = await drive.ensure_manifest_file(
        artifact=proof_artifact, batch_id=batch,
    )
    await drive.verify_downloaded_artifact(
        file_id=stamped.file_id, parent_id=drive.folders.manifests,
        artifact=proof_artifact,
    )
    return {
        "status": "verified_private_source_day", "asset": asset,
        "family": family, "utc_day": month + "-01",
        "source_rows": metrics["source_rows"],
        "source_sha256": data_artifact.sha256,
        "manifest_sha256": proof_artifact.sha256,
        "drive_sha256_readback_verified": True,
        "whole_month_verified": False,
        "historical_2021_coverage_verified": False,
        "model_training_eligible": False,
    }


async def run(*, asset: str, month: str, family: str, root: Path,
              reports: Path, probe_only: bool = False) -> dict:
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
        temp, url = await obtain(
            client, asset=asset, month=month, family=family, root=root, now=now,
        )
    try:
        metrics = verify_gzip_csv(temp, asset=asset, month=month, family=family, now=now)
        if probe_only:
            result = {
                "status": "verified_source_probe", "source": url,
                "sha256": sha(temp.read_bytes()), "asset": asset,
                "family": family, "month": month,
                **metrics, "drive_verified": False,
            }
        else:
            drive = _drive(Settings())
            try:
                result = await publish(
                    drive, source=temp, asset=asset, month=month,
                    family=family, url=url, metrics=metrics, root=root,
                )
            finally:
                await drive.aclose()
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"bitget-tardis-{asset}-{family}-{month}.json").write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n",
        )
        return result
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verified, research-only Bitget Futures Tardis sample day",
    )
    parser.add_argument("--asset", choices=ASSETS, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--family", choices=FAMILIES, required=True)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("data/bitget-tardis"))
    parser.add_argument("--reports", type=Path, default=Path("data/native-reports"))
    options = parser.parse_args(argv)
    try:
        result = asyncio.run(run(
            asset=options.asset, month=options.month, family=options.family,
            root=options.root, reports=options.reports,
            probe_only=options.probe_only,
        ))
    except (HistoryImportError, OSError, httpx.HTTPError) as exc:
        parser.exit(1, f"Bitget Tardis source failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
