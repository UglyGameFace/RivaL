"""Warehouse-wide source-month census. Names are NOT content validation."""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.history_priority import EARLIEST_ARCHIVE_MONTH, window
from parlay_bot.research.publish import _drive

ASSETS = ("BTC", "ETH", "SOL", "XRP")
# Match the actual first supported month in the authoritative downloaders.
# Do not invent pre-publication OKX history for January-June 2023.
START_MONTH = {"binance": EARLIEST_ARCHIVE_MONTH, "bitget": "2021-01",
               "okx": "2023-07", "gate": "2023-01"}
PATTERNS = (
    (r"binance-aux-(BTC|ETH|SOL|XRP)-(mark|index|premium|funding|candles_1m|candles_15m|candles_1h|candles_4h|candles_1d)-(20\d{2}-(?:0[1-9]|1[0-2]))--([a-f0-9]{16})\.manifest\.json", "binance", None),
    (r"(BTC|ETH|SOL|XRP)USDT-5m-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "binance", "candles_5m"),
    (r"okx-(BTC|ETH|SOL|XRP)-USDT-SWAP-USDT-5m-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "okx", "candles_5m"),
    (r"gatebulk-futures_usdt-(BTC|ETH|SOL|XRP)_USDT-(candlesticks_1m|candlesticks_5m|candlesticks_1h|mark_prices|funding_applies|funding_updates|trades)-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "gate", None),
    (r"gate-(BTC|ETH|SOL|XRP)_USDT-USDT-5m-(20\d{2}-\d{2})--([a-f0-9]{16})\.manifest\.json", "gate", "native_candles_5m"),
    (r"bitget-v3-(BTC|ETH|SOL|XRP)-(market|mark|index|premium)-(20\d{2}-(?:0[1-9]|1[0-2]))--([a-f0-9]{16})\.manifest\.json", "bitget", None),
)

# These providers publish paged source evidence. A page is never a complete
# calendar month, even when all listed pages have passed a byte readback.
# OKX official auxiliary archives have provider-issued filenames, not
# normalized source family names. Keep them as *unclassified receipt names*
# until the actual archived manifest and original bytes are read back.
OKX_AUX_RECEIPT = re.compile(
    r"(BTC|ETH|SOL|XRP)-USDT-SWAP-[A-Za-z0-9_.-]{1,110}"
    r"(20\d{2}-(?:0[1-9]|1[0-2]))\.(?:zip|tar\.gz)\.receipt\.json"
)

# Each Gate L2 receipt denotes ONE original hourly file, not a month of
# historical depth or quote availability. Keep this distinct from 5m candles.
GATE_L2_HOURLY_MANIFEST = re.compile(
    r"gate-hourly-l2-(BTC|ETH|SOL|XRP)-(orderbooks|orderbooks_slice)-"
    r"(20[0-9]{8})--([a-f0-9]{16})\.gz\.manifest\.json"
)

# Also index source-native publisher manifest NAMES for the six other
# exchange ledgers. Contract identity and actual byte provenance are checked
# during restore, not claimed by this filename pattern.
OTHER_NATIVE = re.compile(
    r"(hyperliquid|dydx|kucoin|deribit|lighter|phemex)-"
    r"([A-Za-z0-9_-]{2,64})-(USDT|USDC|USD)-5m-"
    r"(20\d{2}-(?:0[1-9]|1[0-2]))--([a-f0-9]{16})\.manifest\.json"
)

BINANCE_METRICS_DAILY = re.compile(
    r"binance-metrics-(BTC|ETH|SOL|XRP)-(20\d{2}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12]\d|3[01]))--([a-f0-9]{16})\.manifest\.json"
)

PAGE_PATTERNS = (
    (r"(hyperliquid|kucoin)-(BTC|ETH|SOL|XRP)-funding-(20\\d{2}-(?:0[1-9]|1[0-2]))-(\\d{2})-([a-f0-9]{16})\\.manifest\\.json", None, "official_funding"),
    (r"dydx-v4-(BTC|ETH|SOL|XRP)-USD-funding-(20\d{2}-(?:0[1-9]|1[0-2]))-(\d{2})-([a-f0-9]{16})\.manifest\.json", "dydx", "funding_with_oracle_price"),
    (r"lighter-(BTC|ETH|SOL|XRP)-(funding|mark_price)-(20\d{2}-(?:0[1-9]|1[0-2]))-(\d{2})-([a-f0-9]{16})\.manifest\.json", "lighter", None),
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
    native = OTHER_NATIVE.fullmatch(name)
    if native:
        venue, contract, _quote, month, sha = native.groups()
        asset = next((a for a in ASSETS if contract.startswith(a)), None)
        if asset is None and contract.startswith("XBT"):
            asset = "BTC"
        if asset is not None:
            return venue, asset, "candles_5m", month, sha
    return None


def parse_page_name(name: str):
    """Recognize original page/section manifests without implying full coverage."""
    for pattern, venue, family in PAGE_PATTERNS:
        match = re.fullmatch(pattern, name)
        if match:
            if family == "official_funding":
                original_venue, asset, month, page, sha = match.groups()
                return original_venue, asset, "funding", month, int(page), sha
            if family is None:
                asset, kind, month, page, sha = match.groups()
            else:
                asset, month, page, sha = match.groups()
                kind = family
            return venue, asset, kind, month, int(page), sha
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


async def list_names(drive, *, max_pages: int = 100) -> list[str]:
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
            if len(names) > 100000:
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
    page_streams = defaultdict(lambda: defaultdict(set))
    name_counts = defaultdict(int)
    okx_aux = defaultdict(set)
    gate_l2_hours = defaultdict(lambda: defaultdict(set))
    binance_oi_days = defaultdict(lambda: defaultdict(set))
    ignored, outside = 0, 0
    current = now.astimezone(UTC).strftime("%Y-%m")
    for name in names:
        name_counts[name] += 1
        key = parse_name(name)
        if key is None:
            page_key = parse_page_name(name)
            if page_key is None:
                metric = BINANCE_METRICS_DAILY.fullmatch(name)
                if metric is not None:
                    asset, day, digest = metric.groups()
                    try:
                        datetime.strptime(day, "%Y-%m-%d")  # noqa: DTZ007 - date format check only
                    except ValueError:
                        outside += 1
                        continue
                    if "2021-01" <= day[:7] < current:
                        binance_oi_days[(asset, day[:7])][day].add(digest)
                    else:
                        outside += 1
                    continue
                gate_l2 = GATE_L2_HOURLY_MANIFEST.fullmatch(name)
                if gate_l2 is not None:
                    asset, family, hour, short_sha = gate_l2.groups()
                    month = hour[:4] + "-" + hour[4:6]
                    if "2021-08" <= month < current:
                        gate_l2_hours[(asset, family, month)][hour].add(short_sha)
                    else:
                        outside += 1
                    continue
                auxiliary = OKX_AUX_RECEIPT.fullmatch(name)
                if auxiliary is not None:
                    asset, month = auxiliary.groups()
                    if "2021-09" <= month < current:
                        okx_aux[(asset, month)].add(name)
                    else:
                        outside += 1
                    continue
                ignored += 1
                continue
            venue, asset, family, month, page, sha = page_key
            if not "2021-01" <= month < current:
                outside += 1
                continue
            page_streams[(venue, asset, family, month)][page].add(sha)
            continue
        venue, asset, family, month, sha = key
        floor = START_MONTH.get(venue, "2021-01")
        if (month < floor or month >= current or month[5:] not in
                tuple(f"{x:02d}" for x in range(1, 13))):
            outside += 1
            continue
        streams[(venue, asset, family)][month].add(sha)
    duplicate_scopes = defaultdict(set)
    for name, count in name_counts.items():
        if count > 1 and (parsed := parse_name(name)) is not None:
            venue, asset, family, month, _ = parsed
            duplicate_scopes[(venue, asset, family)].add(month)
    scopes = []
    for venue, floor, family in (
        ("binance", START_MONTH["binance"], "candles_5m"),
        ("okx", START_MONTH["okx"], "candles_5m"),
        ("gate", START_MONTH["gate"], "candlesticks_5m"),
        *(( "bitget", START_MONTH["bitget"], kind) for kind in ("market", "mark", "index", "premium")),
    ):
        required = months_from(floor, now)
        for asset in ASSETS:
            collected = streams.get((venue, asset, family), {})
            missing = [month for month in required if month not in collected]
            conflicts = [month for month in required
                         if len(collected.get(month, ())) > 1]
            duplicate_names = [month for month in required
                               if month in duplicate_scopes[(venue, asset, family)]]
            scopes.append({
                "venue": venue, "asset": asset, "family": family,
                "source_first_month": floor,
                "start_month_is_attempt_floor_not_listing_proof": venue == "bitget",
                "expected_completed_months": len(required),
                "listed_manifest_months": len(required) - len(missing),
                "not_listed_months": len(missing),
                "first_not_listed": missing[:12],
                "conflicting_months": conflicts[:12],
                "duplicate_name_months": duplicate_names[:12],
                "name_inventory_complete": not missing and not conflicts and not duplicate_names,
                "bytes_reverified_in_this_census": False,
                "model_training_qualified": False,
            })
    partial_pages = [{
        "venue": venue, "asset": asset, "family": family, "utc_month": month,
        "listed_page_numbers": len(pages),
        "conflicting_page_numbers": sorted(
            page for page, shas in pages.items() if len(shas) > 1
        ),
        "full_calendar_month_verified": False,
        "bytes_reverified_in_this_census": False,
        "model_training_qualified": False,
    } for (venue, asset, family, month), pages in sorted(page_streams.items())]
    enrichment = [
        {"venue": v, "asset": a, "family": f, "listed_months": len(months)}
        for (v, a, f), months in sorted(streams.items())
        if f not in ("candles_5m", "candlesticks_5m")
    ]
    priority = window(now)
    selected = set(priority.completed_months)
    # Exact source-month name hints, not SHA-256 readbacks. This separate
    # inventory is required to avoid miscounting 2021 originals as evidence
    # that a selected 2024-2026 month has already been recovered.
    priority_scopes = [{
        "venue": venue, "asset": asset, "family": family,
        "listed_completed_utc_months": sorted(selected.intersection(months)),
        "name_conflict_utc_months": sorted(
            month for month, shas in months.items()
            if month in selected and len(shas) > 1
        ),
        "byte_readbacks_verified_in_this_census": False,
        "complete_source_months_qualified": False,
    } for (venue, asset, family), months in sorted(streams.items())
        if selected.intersection(months)]
    return {
        "schema_version": "rival-drive-source-manifest-census-v1",
        "priority_window": priority.metadata(),
        "priority_24_month_scope_name_hints": priority_scopes,
        "all_completed_scope_name_hints": [{
            "venue": venue, "asset": asset, "family": family,
            "listed_completed_utc_months": sorted(months),
            "byte_readbacks_verified_in_this_census": False,
            "contract_listing_verified": False,
        } for (venue, asset, family), months in sorted(streams.items())],
        "generated_at": now.astimezone(UTC).isoformat(),
        "all_manifest_objects_listed": len(names),
        "unrecognized_manifest_names": ignored,
        "duplicate_manifest_names": sum(n - 1 for n in name_counts.values() if n > 1),
        "out_of_supported_month_range": outside,
        "month_scopes": scopes,
        "page_level_sources_not_full_months": partial_pages,
        "other_family_name_counts": enrichment,
        "binance_daily_metrics_not_full_months": [{
            "venue": "binance", "asset": asset,
            "family": ["open_interest", "positioning"],
            "utc_month": month,
            "distinct_named_days": len(days),
            "duplicate_or_conflicting_day_names": [
                day for day, digests in sorted(days.items())
                if len(digests) > 1
            ][:15],
                        "all_utc_days_present_by_name": len(days) == calendar.monthrange(
                int(month[:4]), int(month[5:]))[1],
            "exact_full_calendar_month_verified": False,
            "original_zip_bytes_rehashed_in_census": False,
        } for (asset, month), days in sorted(binance_oi_days.items())],
        "gate_hourly_l2_not_full_months": [{
            "venue": "gate", "asset": asset, "family": family,
            "utc_month": month, "distinct_named_hours": len(hours),
            "hour_sha_conflicts": sorted(
                hour for hour, shas in hours.items() if len(shas) > 1
            )[:24],
            "full_hourly_source_coverage_verified": False,
            "full_month_source_coverage_verified": False,
            "bytes_reverified_in_this_census": False,
            "historical_book_continuity_qualified": False,
            "model_training_qualified": False,
        } for (asset, family, month), hours in sorted(gate_l2_hours.items())],
        "okx_auxiliary_receipt_name_hints": [{
            "venue": "okx", "asset": asset, "utc_month": month,
            "distinct_original_filenames": len(filenames),
            "source_modules_inferred_from_filename": False,
            "bytes_reverified_in_this_census": False,
            "full_source_month_verified": False,
            "model_training_qualified": False,
        } for (asset, month), filenames in sorted(okx_aux.items())],
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
            "has_conflict": bool(r["conflicting_months"] or r["duplicate_name_months"]),
        } for r in report["month_scopes"] if r["family"] in ("candles_5m", "candlesticks_5m", "market")],
        "source_family_months": [{
            "venue": r["venue"], "asset": r["asset"], "family": r["family"],
            "listed": r["listed_manifest_months"], "missing": r["not_listed_months"],
        } for r in report["month_scopes"]],
        "partial_page_source_months": len(report["page_level_sources_not_full_months"]),
        "okx_auxiliary_name_hint_month_scopes": len(
            report["okx_auxiliary_receipt_name_hints"]
        ),
        "gate_hourly_l2_source_month_scopes": len(
            report["gate_hourly_l2_not_full_months"]
        ),
        "verified_byte_readbacks_in_this_census": 0,
        "live_approved": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
