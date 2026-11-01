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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

HOST = "https://download.gatedata.org"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
TYPES = ("trades", "mark_prices", "funding_updates", "funding_applies")
MAX_BYTES = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_ROWS = 2_000_000


def url_for(*, asset: str, kind: str, month: str) -> str:
    if asset not in ASSETS or kind not in TYPES or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("unsupported Gate asset, type or calendar month")
    compact = month.replace("-", "")
    return f"{HOST}/futures_usdt/{kind}/{compact}/{asset}_USDT-{compact}.csv.gz"


def inspect(payload: bytes, *, asset: str, kind: str, month: str) -> dict:
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
        if not first or len(first) < 2:
            raise ValueError("empty or malformed Gate CSV")
        count = 1
        for row in rows:
            if len(row) != len(first):
                raise ValueError("inconsistent Gate CSV columns")
            count += 1
            if count > MAX_ROWS:
                raise ValueError("Gate row budget exceeded")
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid Gate CSV encoding") from exc
    return {"asset": asset, "kind": kind, "month": month, "rows": count,
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
