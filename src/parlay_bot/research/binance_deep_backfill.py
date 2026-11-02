"""Unattended, official ZIP-only Binance USD-M historical foundation since 2021.

Use the project's existing checksum-verified monthly archive parser and
private Drive readback. No REST candle pagination, no fabricated pre-listing
months, no source blending, and no live forecast/model promotion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.free_history import ASSETS, HistoryImportError, ingest_binance
from parlay_bot.research.history_priority import ordered_months, window
from parlay_bot.research.publish import _artifact, _drive, prepare, publish

FIRST = "2021-01"
MAX_MONTHS_PER_RUN = 32
MAX_LIST_PAGES = 15


def months_since_2021(now: datetime) -> list[str]:
    if now.utcoffset() is None:
        raise ValueError("historical archive calendar must be timezone-aware")
    clock = now.astimezone(UTC)
    end_index = clock.year * 12 + clock.month - 1
    floor = 2021 * 12
    return [
        f"{year:04d}-{month + 1:02d}"
        for year, month in (
            divmod(index, 12) for index in range(floor, end_index)
        )
    ]


async def existing_month_keys(drive) -> set[str]:
    """Read one *bounded* private Drive manifest inventory for resume.

    Presence is only a resume hint. Previous records are NOT counted as
    independently reverified by this run.
    """
    await drive._verify_folder_boundary()
    query = (
        f"'{drive._escape_query(drive.folders.manifests)}' in parents "
        "and trashed = false and name contains 'USDT-5m-' "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    existing = set()
    page_token = None
    for _ in range(MAX_LIST_PAGES):
        params = {
            "q": query, "spaces": "drive", "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,appProperties,parents,trashed)",
        }
        if page_token:
            params["pageToken"] = page_token
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params, headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("private Binance manifest inventory unavailable")
        data = response.json()
        items = data.get("files") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) > 1000:
            raise HistoryImportError("invalid Binance archive manifest listing")
        for row in items:
            if not isinstance(row, dict):
                raise HistoryImportError("invalid Binance manifest identity")
            name = row.get("name")
            props = row.get("appProperties") or {}
            if (not isinstance(name, str)
                or props.get("rival_kind") != "manifest"
                or drive.folders.manifests not in (row.get("parents") or [])):
                raise HistoryImportError("untrusted private Binance manifest listing")
            # The trailing --digest names must originate from the project's
            # immutable research publisher; never assume unrelated files match.
            match = re.fullmatch(
                r"((?:BTC|ETH|SOL|XRP)USDT-5m-20\d{2}-(?:0[1-9]|1[0-2]))"
                r"--[0-9a-f]{16}\.manifest\.json",
                name,
            )
            if match:
                existing.add(match.group(1))
        page_token = data.get("nextPageToken")
        if page_token is None:
            return existing
        if not isinstance(page_token, str) or not page_token:
            raise HistoryImportError("invalid Binance manifest page token")
    raise HistoryImportError("Binance manifest listing exceeded bounded pages")




async def existing_original_keys(drive) -> set[str]:
    """Original ZIP + checksum name pairs, never SHA or byte acceptance.

    Older RivaL normalized archives sometimes lack the provider ZIP entirely.
    Such months must remain in the collection queue even if their candle
    manifest is already listed. Never discard or replace old normalized files.
    """
    await drive._verify_folder_boundary()
    folder = drive.folders.history
    predicate = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        "and name contains 'USDT-5m-' "
        "and appProperties has { key='rival_kind' and value='history' }"
    )
    found: dict[str, set[str]] = {}
    seen = set()
    page = None
    for _ in range(100):
        params = {
            "q": predicate, "spaces": "drive", "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,appProperties,parents,trashed)",
        }
        if page:
            params["pageToken"] = page
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params,
            headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("Binance source-original Drive history inaccessible")
        doc = response.json()
        rows = doc.get("files") if isinstance(doc, dict) else None
        if not isinstance(rows, list) or len(rows) > 1000:
            raise HistoryImportError("invalid source-original Drive inventory")
        for row in rows:
            if (not isinstance(row, dict) or row.get("trashed")
                or folder not in (row.get("parents") or [])
                or not isinstance(row.get("id"), str)
                or not isinstance(row.get("appProperties"), dict)
                or row["appProperties"].get("rival_kind") != "history"):
                raise HistoryImportError("untrusted remote Binance source archive")
            if row["id"] in seen:
                raise HistoryImportError("duplicate historical original object ID")
            seen.add(row["id"])
            name = row.get("name", "")
            match = re.fullmatch(
                r"((?:BTC|ETH|SOL|XRP)USDT-5m-20\d{2}-(?:0[1-9]|1[0-2]))"
                r"\.zip(\.CHECKSUM)?",
                name if isinstance(name, str) else "",
            )
            if match:
                kind = "checksum" if match.group(2) else "zip"
                found.setdefault(match.group(1), set()).add(kind)
        page = doc.get("nextPageToken")
        if page is None:
            return {key for key, kinds in found.items()
                    if kinds == {"zip", "checksum"}}
        if not isinstance(page, str) or not page or len(page) > 1000:
            raise HistoryImportError("invalid original ZIP inventory pagination")
    raise HistoryImportError("source original Drive listing exceeds bounded pages")



def prioritize_missing_source_months(
    months: list[str], assets: tuple[str, ...], available: set[str],
    *, now: datetime | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """First the last 24 completed months, then all older original months.

    Drive manifest names remain unverified resume hints, not proof of
    completed source-grid bytes or of the contract's actual listing.
    """
    ordered = ordered_months(months, now=now)
    planned = [(asset, month) for month in ordered for asset in assets]
    missing = [
        (asset, month) for asset, month in planned
        if f"{asset}USDT-5m-{month}" not in available
    ]
    return missing, len(planned) - len(missing)


async def collect_official_binance(
    *, drive, root: Path, report_dir: Path, now: datetime | None = None,
    assets: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP"),
    max_new: int = 24,
) -> dict:
    clock = now or datetime.now(UTC)
    if (not assets or len(assets) != len(set(assets))
        or any(asset not in ASSETS for asset in assets)
        or not 1 <= max_new <= MAX_MONTHS_PER_RUN):
        raise ValueError("invalid Binance bulk archival scope")
    months = months_since_2021(clock)
    normalized_names = await existing_month_keys(drive)
    original_names = await existing_original_keys(drive)
    # A processed candle month without BOTH provider original objects is NOT
    # complete acquisition. Re-fetch only those missing originals; publish()
    # remains byte-idempotent for existing normalized candle manifests.
    available = normalized_names & original_names
    work, skipped = prioritize_missing_source_months(
        months, assets, available, now=clock,
    )
    downloaded, failed = [], []
    root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    async with asyncio.timeout(9000):
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            for asset, month in work:
                key = f"{asset}USDT-5m-{month}"
                if len(downloaded) >= max_new:
                    break
                try:
                    artifact = await asyncio.to_thread(
                        ingest_binance, client, asset=asset,
                        interval="5m", month=month, out_dir=root,
                        retain_original=True,
                    )
                    quality = audit_partition(artifact.manifest_path)
                    if (quality["month"] != month or quality["venue"] != "binance"
                        or quality["contract"] != asset + "USDT"
                        or quality["interval_minutes"] != 5
                        or not quality["full_calendar_month_verified"]):
                        raise HistoryImportError("source-month incomplete; no publication")
                    # Provider-original bytes are not equivalent to normalized
                    # candle NDJSON. Both must survive private Drive readback.
                    original = artifact.manifest_path.parent / (
                        f"{asset}USDT-5m-{month}.zip")
                    checksum = original.with_name(original.name + ".CHECKSUM")
                    original_item = _artifact(original, 1)
                    if original_item.sha256 != json.loads(
                        artifact.manifest_path.read_text()
                    )["source_sha256"]:
                        raise HistoryImportError("original Binance ZIP SHA changed")
                    for item, label in (
                        (original_item, "original"),
                        (_artifact(checksum, 1), "checksum"),
                    ):
                        remote = await drive.ensure_history_file(
                            artifact=item,
                            batch_id=f"binance-{label}-" + item.sha256[:24],
                            mime_type=("application/zip" if label == "original"
                                       else "text/plain"),
                        )
                        await drive.verify_downloaded_artifact(
                            file_id=remote.file_id,
                            parent_id=drive.folders.history, artifact=item,
                            max_bytes=32 * 1024 * 1024,
                        )
                    prepared = prepare(artifact.manifest_path)
                    receipt = await publish(drive, prepared)
                    if not receipt["restore_sha256_verified"]:
                        raise HistoryImportError("Drive checksum readback unavailable")
                    available.add(key)
                    downloaded.append({
                        "asset": asset, "month": month,
                        "rows": artifact.records,
                        "data_sha256": prepared.history.sha256,
                        "restore_sha256_verified": True,
                        "original_zip_and_checksum_drive_sha_readback": True,
                    })
                except (HistoryImportError, httpx.HTTPError, OSError,
                        RuntimeError, ValueError) as exc:
                    # A missing contract before listing is a permanent source
                    # absence, not a synthetic zero-volume market.
                    failed.append({"asset": asset, "month": month,
                                   "reason": str(exc)[:220]})
    receipt = {
        "schema_version": "rival-binance-2021-official-zip-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "source": "binance", "asset_scope": list(assets),
        "earliest_requested_month": FIRST,
        "latest_complete_month": months[-1] if months else None,
        "planned_asset_months": len(work) + skipped,
        "prioritization": "recent_six_then_remaining_24_months_then_older_missing",
        "priority_window": window(clock).metadata(),
        "newly_published": downloaded,
        "new_months": len(downloaded),
        "remote_manifest_names_skipped_without_readback": skipped,
        "normalized_month_names_missing_original_zip_or_checksum": len(
            normalized_names - original_names
        ),
        "unavailable_or_rejected": failed,
        "fully_verified_2021_to_present": False,
        "live_model_promoted": False, "forward_skill_proven": False,
        "after_cost_profitability_verified": False,
    }
    out = report_dir / "binance-2021-bulk-progress.json"
    out.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n",
                   encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Binance official archive deep backfill")
    parser.add_argument("--max-new", type=int, default=24)
    parser.add_argument("--root", type=Path, default=Path("data/binance-deep"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)
    try:
        drive = _drive(Settings())
        async def worker():
            try:
                return await collect_official_binance(
                    drive=drive, root=args.root, report_dir=args.report_dir,
                    max_new=args.max_new,
                )
            finally:
                await drive.aclose()
        receipt = asyncio.run(worker())
    except (HistoryImportError, ValueError, OSError,
            RuntimeError, httpx.HTTPError) as exc:
        parser.exit(2, f"Binance public deep backfill BLOCKED: {exc}\n")
    print(json.dumps({
        "new_months": receipt["new_months"],
        "skipped_without_readback": receipt["remote_manifest_names_skipped_without_readback"],
        "rejected": len(receipt["unavailable_or_rejected"]),
        "full_2021_coverage_proven": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
