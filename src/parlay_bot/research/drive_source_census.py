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

_SHA64 = re.compile(r"[0-9a-f]{64}")

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
    (r"(hyperliquid|kucoin|phemex|deribit)-(BTC|ETH|SOL|XRP)-funding-(20\d{2}-(?:0[1-9]|1[0-2]))-(\d{2})-([a-f0-9]{16})\.manifest\.json", None, "official_funding"),
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


# KuCoin UTA V2 original pages are partial source evidence, never full months.
KUCOIN_UTA_PAGES = re.compile(
    r"kucoin-uta-v2-(BTC|ETH|SOL|XRP)-"
    r"(candles_1m|candles_15m|candles_1h|candles_4h|candles_1d|mark|index|premium|open_interest)-"
    r"(1min|15min|1hour|4hour|1day)-(20\d{6}T\d{6})-"
    r"([a-f0-9]{16})\.manifest\.json"
)
KUCOIN_INTERVALS = {
    "candles_1m": "1min", "candles_15m": "15min",
    "candles_1h": "1hour", "candles_4h": "4hour",
    "candles_1d": "1day", "mark": "1hour", "index": "1hour",
    "premium": "1hour",
}


def parse_page_name(name: str):
    """Recognize original page/section manifests without implying full coverage."""
    uta = KUCOIN_UTA_PAGES.fullmatch(name)
    if uta:
        asset, family, interval, stamp, digest = uta.groups()
        if interval not in (
            ("1hour", "1day") if family == "open_interest"
            else (KUCOIN_INTERVALS[family],)
        ):
            return None
        try:
            datetime.strptime(stamp, "%Y%m%dT%H%M%S")  # noqa: DTZ007
        except ValueError:
            return None
        return (
            "kucoin", asset, family, stamp[:4] + "-" + stamp[4:6],
            int(stamp[6:8] + stamp[9:]), digest,
        )
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


def summarize(names: list[str], *, now: datetime,
                byte_readback: dict | None = None) -> dict:
    """Name inventory plus exactly what this census byte re-verified.

    ``byte_readback`` is ``{"verified": {(venue, asset, family, month), ...},
    "sampled": [manifest names attempted], "failed": [{"manifest_name",
    "reason"}]}``. A scope may only report ``name_inventory_complete`` when
    its names are complete AND at least one of its months was actually byte
    re-verified in this census -- a names-only run can never claim a scope
    complete. ``bytes_reverified_in_this_census`` is True only for scopes
    with a verified month, and ``byte_readback_months_verified`` names it.
    """
    verified = set((byte_readback or {}).get("verified") or ())
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
            readback_months = sorted(
                month for month in collected
                if (venue, asset, family, month) in verified
            )
            bytes_reverified = bool(readback_months)
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
                # Complete by name AND byte re-verified in this census.
                # A names-only run reports False here, however full the
                # listing looks: names are not content validation.
                "name_inventory_complete": (
                    not missing and not conflicts and not duplicate_names
                    and bytes_reverified
                ),
                "bytes_reverified_in_this_census": bytes_reverified,
                "byte_readback_months_verified": readback_months,
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


async def run(output: Path, *, now: datetime | None = None,
              sample_size: int = 8) -> dict:
    drive = _drive(Settings())
    try:
        names = await list_names(drive)
        readback = await sampled_byte_readback(
            drive, names, sample_size=sample_size,
        )
    finally:
        await drive.aclose()
    report = summarize(names, now=now or datetime.now(UTC),
                       byte_readback=readback)
    # State exactly what this census re-verified: sampled manifest bytes
    # only. Normalized data files and provider originals are NOT re-read
    # here (history_byte_audit does the full Binance 5m readback).
    report["byte_readback_sample_size"] = len(readback["sampled"])
    report["byte_readback_sampled_manifest_names"] = readback["sampled"]
    report["byte_readback_months_verified"] = sorted(
        [list(key) for key in readback["verified"]]
    )
    report["byte_readback_failed"] = readback["failed"]
    report["byte_readback_scope"] = (
        "sampled manifest bytes only, SHA-verified against Drive "
        "appProperties.rival_sha256 and tied to the listed name; normalized "
        "data files and provider originals are NOT re-read by this census"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8")
    return report


def choose_readback_sample(names: list[str], *, sample_size: int = 8) -> list[str]:
    """Deterministic stride sample of parseable month manifests to re-read.

    Bounded (1..16) and reproducible: the same listing always samples the
    same names, spread across the whole listing rather than one venue.
    """
    if not 1 <= sample_size <= 16:
        raise ValueError("bounded byte-readback sample required")
    candidates = sorted({name for name in names if parse_name(name) is not None})
    if len(candidates) <= sample_size:
        return candidates
    step = len(candidates) / sample_size
    picked = [candidates[min(len(candidates) - 1, int(i * step))]
              for i in range(sample_size)]
    return sorted(set(picked))


def _check_readback_manifest(info: dict, parsed: tuple) -> None:
    """Tie one re-read manifest's content to its listed name.

    Publisher suffix conventions differ (data digest for Binance/Bitget,
    compressed-archive hash for Gate), so no single suffix rule is assumed.
    What is checked: the manifest is an object, its venue/contract/month
    (when present) match the name, its ``file`` (when present) carries the
    name's short hash, and at least one content hash it declares is a valid
    SHA-256 (any hash it declares must be valid).
    """
    venue, asset, _family, month, short_sha = parsed
    if not isinstance(info, dict):
        raise HistoryImportError("remote source manifest must be an object")
    if "venue" in info and info["venue"] != venue:
        raise HistoryImportError("remote manifest venue differs from its name")
    contract = info.get("contract")
    if isinstance(contract, str) and asset not in contract:
        raise HistoryImportError("remote manifest contract differs from its name")
    if "month" in info and info["month"] != month:
        raise HistoryImportError("remote manifest month differs from its name")
    filename = info.get("file")
    if isinstance(filename, str) and not filename.endswith(
        "--" + short_sha + ".ndjson"
    ):
        raise HistoryImportError("remote manifest file differs from its name")
    declared = [info.get(key) for key in
                ("data_sha256", "source_sha256", "raw_sha256")]
    present = [sha for sha in declared if sha is not None]
    if (not present or any(
        not isinstance(sha, str) or _SHA64.fullmatch(sha) is None
        for sha in present
    )):
        raise HistoryImportError("remote manifest declares no valid content hash")


async def sampled_byte_readback(drive, names: list[str], *,
                               sample_size: int = 8) -> dict:
    """Re-read a bounded sample of listed manifests and SHA-verify them.

    Each sampled manifest's bytes are downloaded through the trusted
    manifest path (bytes SHA-checked against Drive ``appProperties``) and
    tied to its listed name. Returns ``{"verified": {(venue, asset, family,
    month), ...}, "sampled": [names attempted], "failed": [{"manifest_name",
    "reason"}]}``. A failed readback is recorded, never treated as verified,
    and never aborts the census: the report states exactly what was verified.
    """
    # Deferred import: source_manifests imports list_names from this module.
    from parlay_bot.research.source_manifests import _download_manifest

    sampled = choose_readback_sample(names, sample_size=sample_size)
    verified, failed = set(), []
    for name in sampled:
        parsed = parse_name(name)
        try:
            info = await _download_manifest(drive, name=name)
            _check_readback_manifest(info, parsed)
        except Exception as exc:  # noqa: BLE001 - a failed readback is data
            failed.append({"manifest_name": name, "reason": str(exc)[:180]})
            continue
        verified.add(parsed[:4])
    return {"verified": verified, "sampled": sampled, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read actual cumulative warehouse manifest names")
    parser.add_argument("--out", type=Path,
                        default=Path("data/native-reports/drive-source-census.json"))
    parser.add_argument("--sample-size", type=int, default=8,
                        help="manifests byte re-read in this census (1-16)")
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.out, sample_size=args.sample_size))
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
        "verified_byte_readbacks_in_this_census": len(
            report["byte_readback_months_verified"]
        ),
        "byte_readback_sampled": report["byte_readback_sample_size"],
        "byte_readback_failed": len(report["byte_readback_failed"]),
        "live_approved": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
