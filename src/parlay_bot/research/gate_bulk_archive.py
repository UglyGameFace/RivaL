"""Official Gate USDT-perpetual monthly bulk archive acquisition, research-only.

This downloads public files, never scrapes protected endpoints, and never
fills missing months with another venue's data.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx

HOST = "https://download.gatedata.org"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
TYPES = ("trades", "mark_prices", "funding_updates", "funding_applies",
         "candlesticks_1m", "candlesticks_5m", "candlesticks_1h")
# Actual Gate futures downloads are normally headerless CSV rows.
# Source: https://www.gate.com/developer/historical_quotes
SCHEMA = {
    "trades": ("timestamp", "dealid", "price", "size"),
    "mark_prices": ("timestamp", "index_price", "mark_price", "last_price"),
    "funding_updates": ("timestamp", "funding_rate", "interest_rate",
                        "bid_diff", "ask_diff", "mark_price", "index_price",
                        "update_count"),
    "funding_applies": ("timestamp", "funding_rate"),
    "candlesticks_1m": ("timestamp", "size", "close", "high", "low", "open"),
    "candlesticks_5m": ("timestamp", "size", "close", "high", "low", "open"),
    "candlesticks_1h": ("timestamp", "size", "close", "high", "low", "open"),
}
MAX_BYTES = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_ROWS = 2_000_000


def url_for(*, asset: str, kind: str, month: str) -> str:
    if asset not in ASSETS or kind not in TYPES or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("unsupported Gate asset, type or calendar month")
    compact = month.replace("-", "")
    return f"{HOST}/futures_usdt/{kind}/{compact}/{asset}_USDT-{compact}.csv.gz"


def _finite(raw: str, *, allow_negative: bool = False) -> float:
    try:
        n = float(raw)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError("Gate CSV has invalid numeric field") from exc
    if not math.isfinite(n) or (not allow_negative and n < 0):
        raise ValueError("Gate CSV has nonfinite or disallowed negative field")
    return n


def _row_time(raw: str, *, month: str) -> float:
    ts = _finite(raw, allow_negative=False)
    first = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
    # Gate's export calendar may be Beijing-local; keep the eight-hour
    # boundary tolerance visible rather than mislabel it full UTC coverage.
    if not first.timestamp() - 8 * 3600 <= ts < end.timestamp() + 8 * 3600:
        raise ValueError("Gate record timestamp outside source archive month")
    return ts


def _validate_row(row: list[str], *, kind: str, month: str) -> float:
    fields = SCHEMA[kind]
    if len(row) != len(fields) or any(not value.strip() for value in row):
        raise ValueError("Gate CSV source schema mismatch")
    ts = _row_time(row[0], month=month)
    for i, name in enumerate(fields[1:], start=1):
        # Signed trade position size and funding rate/differences are valid.
        val = _finite(row[i], allow_negative=name in {
            "size", "funding_rate", "interest_rate", "bid_diff", "ask_diff",
        })
        if name in {"price", "index_price", "mark_price", "last_price",
                    "open", "high", "low", "close"} and val <= 0:
            raise ValueError("Gate CSV contains nonpositive price")
        if name in {"dealid", "update_count"} and (val < 0 or not val.is_integer()):
            raise ValueError("Gate CSV contains invalid source counter")
    if kind.startswith("candlesticks_"):
        _, size, close, high, low, opened = (
            _finite(row[i], allow_negative=(i == 1)) for i in range(6)
        )
        if not low <= min(opened, close) <= max(opened, close) <= high:
            raise ValueError("Gate CSV has invalid candle OHLC envelope")
    return ts


def inspect(payload: bytes, *, asset: str, kind: str, month: str) -> dict:
    # Check request identity even when called without URL construction.
    url_for(asset=asset, kind=kind, month=month)
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError("archive byte budget exceeded")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            expanded = stream.read(MAX_EXPANDED + 1)
    except (OSError, EOFError) as exc:
        raise ValueError("invalid Gate gzip archive") from exc
    if len(expanded) > MAX_EXPANDED:
        raise ValueError("expanded archive budget exceeded")
    try:
        rows = csv.reader(io.StringIO(expanded.decode("utf-8-sig"), newline=""))
        first = next(rows, None)
        if not first:
            raise ValueError("empty Gate CSV")
        # Official raw exports contain data, not necessarily a header.
        # Accept a header only when it exactly matches the documented schema.
        fields = SCHEMA[kind]
        has_header = tuple(v.strip().lower() for v in first) == fields
        count = 0
        minimum = None
        maximum = None
        from itertools import chain
        for row in (rows if has_header else chain((first,), rows)):
            ts = _validate_row(row, kind=kind, month=month)
            minimum = ts if minimum is None else min(minimum, ts)
            maximum = ts if maximum is None else max(maximum, ts)
            count += 1
            if count > MAX_ROWS:
                raise ValueError("Gate row budget exceeded")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid Gate CSV encoding") from exc
    if count == 0:
        raise ValueError("empty Gate data archive")
    return {"asset": asset, "kind": kind, "month": month, "rows": count,
            "has_header": has_header,
            "first_source_timestamp": minimum,
            "last_source_timestamp": maximum,
            "compressed_sha256": hashlib.sha256(payload).hexdigest(),
            "compressed_bytes": len(payload), "expanded_bytes": len(expanded),
            "status": "downloaded_schema_checked_research_only",
            "time_alignment_verified": False,
            "source_native_model_eligible": False}

def download(*, client: httpx.Client, asset: str, kind: str, month: str,
             output: Path) -> dict:
    url = url_for(asset=asset, kind=kind, month=month)
    if output.exists():
        raise ValueError("immutable source archive already exists")
    with client.stream("GET", url, follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f"Gate public archive unavailable: HTTP {response.status_code}")
        parts = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("Gate compressed download exceeds byte budget")
            parts.append(chunk)
    payload = b"".join(parts)
    receipt = inspect(payload, asset=asset, kind=kind, month=month)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    receipt.update({"source_url": url, "retrieved_at": datetime.now(UTC).isoformat(),
                    "raw_path": str(output)})
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate official monthly raw research archive")
    parser.add_argument("--asset", required=True, choices=ASSETS)
    parser.add_argument("--kind", required=True, choices=TYPES)
    parser.add_argument("--month", required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("data/gate-native"))
    args = parser.parse_args()
    url_for(asset=args.asset, kind=args.kind, month=args.month)
    output = args.out_dir / args.kind / args.month / f"{args.asset}_USDT-{args.month}.csv.gz"
    with httpx.Client(timeout=60) as client:
        result = download(client=client, asset=args.asset, kind=args.kind,
                          month=args.month, output=output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
