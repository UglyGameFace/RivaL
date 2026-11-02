"""Warehouse-wide source-month census. Names are NOT content validation."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _drive

ASSETS = ("BTC", "ETH", "SOL", "XRP")
PATTERNS = (
    (r"(BTC|ETH|SOL|XRP)USDT-5m-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "binance", "candles_5m"),
    (r"okx-(BTC|ETH|SOL|XRP)-USDT-SWAP-USDT-5m-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "okx", "candles_5m"),
    (r"gatebulk-futures_usdt-(BTC|ETH|SOL|XRP)_USDT-(candlesticks_1m|candlesticks_5m|candlesticks_1h|mark_prices|funding_applies|funding_updates|trades)-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "gate", None),
    (r"gate-(BTC|ETH|SOL|XRP)_USDT-USDT-5m-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "gate", "native_candles_5m"),
)


def parse_name(name: str):
    for pattern, venue, family in PATTERNS:
        m = re.fullmatch(pattern, name)
        if m:
            if family is None:
                asset, kind, month, sha = m.groups()
            else:
                asset, month, sha = m.groups()
                kind = family
            return venue, asset, kind, month, sha
    return None


def months_from(start: str, now: datetime) -> list[str]:
    if now.utcoffset() is None:
        raise ValueError("clock must be timezone aware")
    yy, mm = map(int, start.split("-"))
    clock = now.astimezone(UTC)
    out = []
    while (yy, mm) < (clock.year, clock.month):
        out.append(f"{yy:04d}-{mm:02d}")
        yy, mm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
    return out


async def list_names(drive, *, max_pages: int = 20) -> list[str]:
    """Fail closed on incomplete pagination, duplicate IDs or untrusted folder."""
    await drive._verify_folder_boundary()
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    seen, names, cursor = set(), [], None
    for _ in range(max_pages):
        params = {"q": query, "spaces": "drive", "pageSize": 1000,
                  "fields": "nextPageToken,files(id,name,parents,trashed,appProperties)"}
        if cursor:
            params["pageToken"] = cursor
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params, headers=await drive._headers()
        )
        if response.status_code != 200:
            raise HistoryImportError("warehouse source manifest listing unavailable")
        result = response.json()
        files = result.get("files") if isinstance(result, dict) else None
        if not isinstance(files, list) or len(files) > 1000:
            raise HistoryImportError("invalid warehouse source manifest page")
        for row in files:
            if not isinstance(row, dict):
                raise HistoryImportError("invalid source manifest row")
            ident, name = row.get("id"), row.get("name")
            props = row.get("appProperties")
            if (row.get("trashed") or folder not in (row.get("parents") or [])
                or not isinstance(ident, str) or not ident or ident in seen
                or not isinstance(name, str) or not 1 <= len(name) <= 240
                or not isinstance(props, dict)
                or props.get("rival_kind") != "manifest"):
                raise HistoryImportError("duplicate or foreign source manifest item")
            seen.add(ident)
            names.append(name)
            if len(names) > 20000:
                raise HistoryImportError("warehouse manifest census cap exceeded")
        next_page = result.get("nextPageToken")
        if next_page is None:
            return names
        if (not isinstance(next_page, str) or not 1 <= len(next_page) <= 1000
            or not next_page.isascii() or not next_page.isprintable()
            or next_page == cursor):
            raise HistoryImportError("invalid warehouse source pagination")
        cursor = next_page
    raise HistoryImportError("warehouse listing incomplete; cannot report coverage")


def summarize(names: list[str], *, now: datetime) -> dict:
    streams = defaultdict(lambda: defaultdict(set))
    ignored, outside = 0, 0
    current = now.astimezone(UTC).strftime("%Y-%m")
    for name in names:
        key = parse_name(name)
        if key is None:
            ignored += 1
            continue
        venue, asset, family, month, sha = key
        floor = "2021-01" if venue == "binance" else "2023-01"
        if (month < floor or month >= current or month[5:] not in
                tuple(f"{x:02d}" for x in range(1, 13))):
            outside += 1
            continue
        streams[(venue, asset, family)][month].add(sha)
    scopes = []
    for venue, floor, family in (
        ("binance", "2021-01", "candles_5m"),
        ("okx", "2023-01", "candles_5m"),
        ("gate", "2023-01", "candlesticks_5m"),
    ):
        required = months_from(floor, now)
        for asset in ASSETS:
            collected = streams.get((venue, asset, family), {})
            missing = [month for month in required if month not in collected]
            conflicts = [month for month in required
                         if len(collected.get(month, ())) > 1]
            scopes.append({
                "venue": venue, "asset": asset, "family": family,
                "expected_completed_months": len(required),
                "listed_manifest_months": len(required) - len(missing),
                "not_listed_months": len(missing),
                "first_not_listed": missing[:12],
                "conflicting_months": conflicts[:12],
                "name_inventory_complete": not missing and not conflicts,
                "bytes_reverified_in_this_census": False,
                "model_training_qualified": False,
            })
    enrichment = [
        {"venue": v, "asset": a, "family": f, "listed_months": len(months)}
        for (v, a, f), months in sorted(streams.items())
        if f not in ("candles_5m", "candlesticks_5m")
    ]
    return {
        "schema_version": "rival-drive-source-manifest-census-v1",
        "generated_at": now.astimezone(UTC).isoformat(),
        "all_manifest_objects_listed": len(names),
        "unrecognized_manifest_names": ignored,
        "out_of_supported_month_range": outside,
        "month_scopes": scopes,
        "other_family_name_counts": enrichment,
        "warning": "Drive metadata names only; not byte SHA revalidation, complete source data, historic listing-date proof, model admission, or after-cost profitability.",
        "all_source_families_2021_to_present_verified": False,
        "model_approved": False,
    }


async def run(output: Path, *, now: datetime | None = None) -> dict:
    drive = _drive(Settings())
    try:
        names = await list_names(drive)
    finally:
        await drive.aclose()
    report = summarize(names, now=now or datetime.now(UTC))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read actual cumulative warehouse manifest names")
    parser.add_argument("--out", type=Path,
                        default=Path("data/native-reports/drive-source-census.json"))
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.out))
    print(json.dumps({
        "total_manifest_names": report["all_manifest_objects_listed"],
        "per_asset_5m": [{
            "venue": r["venue"], "asset": r["asset"],
            "listed": r["listed_manifest_months"],
            "not_listed": r["not_listed_months"],
            "has_conflict": bool(r["conflicting_months"]),
        } for r in report["month_scopes"]],
        "verified_byte_readbacks_in_this_census": 0,
        "live_approved": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
