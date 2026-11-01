"""Bounded, source-exact OKX auxiliary archive ingestion for offline research.

Never equate catalog discovery, a file checksum, or retrospective features with
forward observations, executable quotes, or validated model improvement.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import tarfile
import tempfile
import zipfile
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.official_okx_bootstrap import ASSETS, month_bounds

MODULES = frozenset({"trades", "funding_rates", "orderbook_l2_400", "orderbook_l2_5000"})
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 160 * 1024 * 1024
MAX_RECORDS = 2_000_000
MAX_MEMBERS = 16


def archive_url(*, asset: str, month: str, record: dict) -> str:
    """Validate the entire untrusted catalog entry before network or disk I/O."""
    if asset not in ASSETS:
        raise HistoryImportError("unknown OKX asset")
    month_bounds(month)
    if not isinstance(record, dict):
        raise HistoryImportError("malformed OKX catalog entry")
    name, url = record.get("filename"), record.get("source_url")
    if not isinstance(name, str) or not isinstance(url, str):
        raise HistoryImportError("missing archive filename or URL")
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "static.okx.com"
        or parsed.port not in (None, 443) or parsed.username or parsed.password
        or parsed.query or parsed.fragment or unquote(parsed.path) != parsed.path
        or not parsed.path.startswith("/cdn/okex/")
        or name != Path(name).name or ".." in name or "\\" in name
        or not name.startswith(f"{asset}-USDT-SWAP-")
        or not (name.endswith(".zip") or name.endswith(".tar.gz"))
        or not parsed.path.endswith("/" + name)
        or (month not in name and month.replace("-", "") not in name)):
        raise HistoryImportError("untrusted OKX archive URL or instrument/month")
    return url


def _positive(value: object, *, zero_allowed: bool = False) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryImportError("invalid OKX auxiliary numeric value") from exc
    if not math.isfinite(num) or num < 0 or (not zero_allowed and num == 0):
        raise HistoryImportError("invalid OKX auxiliary numeric range")
    return num


def _event_time(value: object, start: datetime, end: datetime) -> int:
    """Treat timestamps as UTC milliseconds only; no invented conversion."""
    value = str(value)
    if len(value) != 13 or not value.isascii() or not value.isdecimal():
        raise HistoryImportError("ambiguous OKX archive timestamp unit")
    timestamp = int(value)
    # Monthly UTC+8 files can contain adjacent UTC-day boundary events.
    if not int((start - timedelta(days=1)).timestamp() * 1000) <= timestamp < int(
        (end + timedelta(days=1)).timestamp() * 1000
    ):
        raise HistoryImportError("event lies outside OKX monthly archive tolerance")
    return timestamp


def _col(row: dict, names: tuple[str, ...]):
    # A legacy bilingual header is deliberately NOT guessed.
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    raise HistoryImportError("unsupported OKX archive schema: " + ",".join(names))


def _parse_member(name: str, stream, *, asset: str, module: str, start: datetime,
                  end: datetime, buckets: dict, count: list[int]) -> None:
    if name.endswith(".csv"):
        text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
        try:
            reader = csv.DictReader(text)
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise HistoryImportError("missing/duplicate archive CSV headers")
            for row in reader:
                if not row or None in row:
                    raise HistoryImportError("malformed OKX CSV record")
                for field in ("instId", "inst_id", "symbol"):
                    if field in row and row[field] and row[field] != f"{asset}-USDT-SWAP":
                        raise HistoryImportError("wrong swap identity inside archive")
                count[0] += 1
                if count[0] > MAX_RECORDS:
                    raise HistoryImportError("archive exceeds sample row budget")
                if module == "trades":
                    ts = _event_time(_col(row, ("ts", "timestamp", "created_time")), start, end)
                    side = str(_col(row, ("side",))).lower()
                    if side not in {"buy", "sell"}:
                        raise HistoryImportError("unknown OKX aggressor side")
                    _positive(_col(row, ("px", "price")))
                    size = _positive(_col(row, ("sz", "size")), zero_allowed=True)
                    slot = ts // 300_000 * 300_000
                    bucket = buckets[slot]
                    bucket["trades"] += 1
                    bucket["buy_size_source_units" if side == "buy" else "sell_size_source_units"] += size
                elif module == "funding_rates":
                    ts = _event_time(_col(row, ("fundingTime", "funding_time", "ts")), start, end)
                    rate = float(_col(row, ("fundingRate", "funding_rate", "realizedRate")))
                    if not math.isfinite(rate) or abs(rate) > 1:
                        raise HistoryImportError("invalid historical funding rate")
                    # Funding at the settlement instant is usable only AFTER that event.
                    slot = ts // 300_000 * 300_000
                    bucket = buckets[slot]
                    if "funding_rate" in bucket and bucket["funding_rate"] != rate:
                        raise HistoryImportError("conflicting funding settlements")
                    bucket["funding_rate"] = rate
                else:
                    raise HistoryImportError("L2 CSV schema requires separate verification")
        finally:
            text.detach()
    elif name.endswith((".jsonl", ".ndjson")) and module.startswith("orderbook_l2_"):
        text = io.TextIOWrapper(stream, encoding="utf-8")
        try:
            for line in text:
                count[0] += 1
                if count[0] > MAX_RECORDS or len(line) > 2_000_000:
                    raise HistoryImportError("oversized L2 record")
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise HistoryImportError("invalid L2 JSON") from exc
                if not isinstance(event, dict):
                    raise HistoryImportError("invalid L2 event")
                ts = _event_time(event.get("ts"), start, end)
                action = event.get("action")
                if action not in {"snapshot", "update"}:
                    raise HistoryImportError("L2 action unsupported")
                # Incremental updates cannot be priced as full books.
                if action != "snapshot":
                    continue
                asks, bids = event.get("asks"), event.get("bids")
                if not isinstance(asks, list) or not isinstance(bids, list) or not asks or not bids:
                    raise HistoryImportError("incomplete L2 snapshot")
                ask = min(_positive(level[0]) for level in asks if isinstance(level, list) and len(level) >= 2)
                bid = max(_positive(level[0]) for level in bids if isinstance(level, list) and len(level) >= 2)
                if bid >= ask:
                    raise HistoryImportError("crossed L2 snapshot")
                slot = ts // 300_000 * 300_000
                bucket = buckets[slot]
                bucket["l2_snapshot_count"] += 1
                if ts >= bucket.get("last_snapshot_ts_ms", -1):
                    bucket["last_snapshot_ts_ms"] = ts
                    bucket["last_snapshot_spread_bps"] = 20_000 * (ask - bid) / (ask + bid)
        finally:
            text.detach()
    else:
        raise HistoryImportError("unrecognized OKX auxiliary file schema")


def inspect_archive(path: Path, *, asset: str, month: str, module: str) -> dict:
    """Validate bounded decompression and schema, derive research-only 5m events."""
    if module not in MODULES or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise HistoryImportError("unapproved or oversized archive")
    start, end = month_bounds(month)
    buckets = defaultdict(lambda: defaultdict(float))
    count = [0]
    total = 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_MEMBERS:
                raise HistoryImportError("invalid ZIP member count")
            for entry in entries:
                if (entry.is_dir() or entry.flag_bits & 1 or not entry.filename
                    or Path(entry.filename).name != entry.filename or ".." in entry.filename
                    or "\\" in entry.filename or entry.file_size > MAX_EXPANDED_BYTES):
                    raise HistoryImportError("unsafe ZIP member")
                total += entry.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise HistoryImportError("ZIP decompression budget exceeded")
                with archive.open(entry) as source:
                    _parse_member(entry.filename, source, asset=asset, module=module, start=start,
                                  end=end, buckets=buckets, count=count)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path, mode="r:gz") as archive:
            entries = archive.getmembers()
            if not 1 <= len(entries) <= MAX_MEMBERS:
                raise HistoryImportError("invalid tar member count")
            for entry in entries:
                if (not entry.isfile() or not entry.name or Path(entry.name).name != entry.name
                    or ".." in entry.name or "\\" in entry.name
                    or entry.size > MAX_EXPANDED_BYTES):
                    raise HistoryImportError("unsafe tar member")
                total += entry.size
                if total > MAX_EXPANDED_BYTES:
                    raise HistoryImportError("tar decompression budget exceeded")
                source = archive.extractfile(entry)
                if source is None:
                    raise HistoryImportError("missing tar file content")
                with source:
                    _parse_member(entry.name, source, asset=asset, module=module, start=start,
                                  end=end, buckets=buckets, count=count)
    else:
        raise HistoryImportError("unsupported OKX archive encoding")
    if not count[0] or not buckets:
        raise HistoryImportError("OKX archive yielded no recognizable research features")
    output = []
    for ms, features in sorted(buckets.items()):
        when = datetime.fromtimestamp(ms / 1000, UTC)
        if not start <= when < end:
            continue
        output.append({"venue": "okx", "contract": f"{asset}-USDT-SWAP",
                       "event_bucket_open": when.isoformat(),
                       "available_after": (when + timedelta(minutes=5)).isoformat(),
                       "features": dict(features)})
    if not output:
        raise HistoryImportError("no OKX events fell within the requested UTC month")
    return {"parsed_rows": count[0], "feature_buckets": len(output),
            "expanded_bytes": total, "features": output,
            "l2_full_book_reconstructed": False,
            "full_month_coverage_qualified": False}


async def ingest_one(*, client: httpx.AsyncClient, asset: str, month: str,
                     module: str, record: dict, root: Path,
                     max_bytes: int = MAX_ARCHIVE_BYTES) -> dict:
    """One archive per invocation; never overwrite historical evidence."""
    if module not in MODULES or not 1 <= max_bytes <= MAX_ARCHIVE_BYTES:
        raise HistoryImportError("unsupported archive request or byte budget")
    url = archive_url(asset=asset, month=month, record=record)
    root.mkdir(parents=True, exist_ok=True)
    name = record["filename"]
    target = root / name
    if target.exists():
        raise HistoryImportError("archive already exists; refusing evidence replacement")
    digest = hashlib.sha256()
    fd, tmpname = tempfile.mkstemp(prefix=".okx-", dir=root)
    try:
        with os.fdopen(fd, "wb") as output:
            async with client.stream("GET", url, follow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise HistoryImportError("archive request not successful")
                length = response.headers.get("content-length")
                if length and (not length.isdecimal() or int(length) > max_bytes):
                    raise HistoryImportError("OKX archive exceeds allowed transfer")
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise HistoryImportError("OKX archive byte limit exceeded")
                    digest.update(chunk)
                    output.write(chunk)
        if total == 0:
            raise HistoryImportError("empty OKX archive")
        observed = datetime.now(UTC).isoformat()
        parsed = inspect_archive(Path(tmpname), asset=asset, month=month, module=module)
        # Every retained output is retrospective and feature availability is
        # event-time only; true first observation is this retrieval.
        features = root / (name + ".research.ndjson")
        receipt_path = root / (name + ".receipt.json")
        if features.exists() or receipt_path.exists():
            raise HistoryImportError("research receipt already exists")
        feature_bytes = "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
                                for row in parsed.pop("features")).encode("utf-8")
        receipt = {"schema_version": "okx-auxiliary-research-v1",
                   "venue": "okx", "contract": f"{asset}-USDT-SWAP",
                   "month": month, "module": module, "source_url": url,
                   "archive_sha256": digest.hexdigest(), "archive_bytes": total,
                   "first_observed_at": observed,
                   "status": "downloaded_schema_checked_research_only",
                   "source_provider_checksum_verified": False,
                   "prospective_forecast": False, "live_model_import_allowed": False,
                   "live_quotes_verified": False, "after_cost_edge_verified": False,
                   "feature_sha256": hashlib.sha256(feature_bytes).hexdigest(),
                   **parsed}
        # Preserve raw immutable archive, feature sidecar, and provenance.
        os.replace(tmpname, target)
        features.write_bytes(feature_bytes)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n",
                                encoding="utf-8")
        return receipt
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded OKX archive research import")
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--module", choices=sorted(MODULES), required=True)
    parser.add_argument("--max-mib", type=int, default=32)
    parser.add_argument("--root", type=Path, default=Path("data/okx-auxiliary"))
    args = parser.parse_args(argv)
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        asset, month = inventory["asset"], inventory["month"]
        entry = inventory["modules"][args.module]
        if entry["status"] != "cataloged_not_downloaded" or len(entry["files"]) != 1:
            raise HistoryImportError("catalog must contain exactly one unimported archive")
        if not 1 <= args.max_mib <= 32:
            raise HistoryImportError("invalid archive transfer budget")
        async def worker():
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                return await ingest_one(
                    client=client, asset=asset, month=month, module=args.module,
                    record=entry["files"][0], root=args.root,
                    max_bytes=args.max_mib * 1024 * 1024,
                )
        result = asyncio.run(worker())
    except (OSError, KeyError, TypeError, ValueError, httpx.HTTPError,
            HistoryImportError) as exc:
        parser.exit(2, f"OKX auxiliary archive UNVERIFIED: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
