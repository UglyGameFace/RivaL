"""Independent private Drive SHA/calendar re-audit of historic Binance 5m months.

Unlike the cumulative source census, only this job reads the actual warehouse
bytes. Verified normalized candles are separate from original provider ZIPs.
It never silently treats a missing original as a complete raw-source archive.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.drive_source_census import list_names
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive, prepare

PATTERN = re.compile(
    r"(BTC|ETH|SOL|XRP)USDT-5m-(20\d{2}-(?:0[1-9]|1[0-2]))"
    r"--([a-f0-9]{16})\.manifest\.json"
)
SHA = re.compile("[0-9a-f]{64}")


def choose(names: list[str], *, now: datetime, limit: int = 4) -> list[str]:
    if now.utcoffset() is None or not 1 <= limit <= 16:
        raise ValueError("bounded UTC source re-audit required")
    matches = sorted({
        name for name in names if PATTERN.fullmatch(name)
    })
    if not matches:
        return []
    # Deterministic rotation, not permanently rereading the same four recent
    # months. Every existing source month receives eventual independent audit.
    offset = (int(now.timestamp()) // (12 * 3600) * limit) % len(matches)
    return (matches[offset:] + matches[:offset])[:limit]


async def _get_source(drive, *, file_id: str,
                      expected_sha: str, size: int, max_bytes: int) -> bytes:
    if (not file_id or len(file_id) > 128
        or any(x not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
               for x in file_id)
        or SHA.fullmatch(expected_sha) is None
        or not 0 < size <= max_bytes):
        raise HistoryImportError("untrusted Drive history object")
    response = await drive._client.get(
        f"{drive.DRIVE_URL}/files/{file_id}",
        params={"alt": "media"}, headers=await drive._headers(),
        follow_redirects=False,
    )
    if (response.status_code != 200 or len(response.content) != size
        or hashlib.sha256(response.content).hexdigest() != expected_sha):
        raise HistoryImportError("actual remote source byte SHA mismatch")
    return response.content


async def verify_month(drive, *, name: str, root: Path) -> dict:
    match = PATTERN.fullmatch(name)
    if match is None:
        raise HistoryImportError("source auditor called with non-Binance month")
    asset, month, short_sha = match.groups()
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        f"and name = '{drive._escape_query(name)}'"
    )
    response = await drive._client.get(
        f"{drive.DRIVE_URL}/files", params={
            "q": query, "spaces": "drive", "pageSize": 20,
            "fields": "nextPageToken,files(id,name,size,parents,trashed,appProperties)",
        }, headers=await drive._headers(), follow_redirects=False,
    )
    if response.status_code != 200:
        raise HistoryImportError("remote source manifest lookup failed")
    payload = response.json()
    files = payload.get("files") if isinstance(payload, dict) else None
    if (not isinstance(files, list) or len(files) != 1
        or payload.get("nextPageToken")):
        raise HistoryImportError("missing, duplicated or unbounded manifest")
    entry = files[0]
    props = entry.get("appProperties") or {}
    sha = props.get("rival_sha256")
    if (entry.get("name") != name or entry.get("trashed")
        or folder not in entry.get("parents", [])
        or props.get("rival_kind") != "manifest"
        or not isinstance(sha, str)):
        raise HistoryImportError("manifest outside trusted warehouse")
    contents = await _get_source(
        drive, file_id=entry["id"],
        expected_sha=sha, size=int(entry["size"]), max_bytes=8192,
    )
    info = json.loads(contents)
    if (info.get("schema_version") != "rival-research-history-v1"
        or info.get("source") != "binance_um_klines"
        or info.get("venue") != "binance"
        or info.get("contract") != asset + "USDT"
        or info.get("interval_minutes") != 5
        or info.get("checksum_verified") is not True
        or info.get("source_url") != (
            "https://data.binance.vision/data/futures/um/monthly/klines/"
            f"{asset}USDT/5m/{asset}USDT-5m-{month}.zip")
        or not isinstance(info.get("file"), str)
        or not info["file"].endswith("--" + short_sha + ".ndjson")):
        raise HistoryImportError("remote month identity/provenance invalid")
    # Original research publisher uses a stable identity hash for both the
    # NDJSON and the manifest. Never assume a file name proves its body.
    data_sha = info.get("data_sha256")
    if not isinstance(data_sha, str) or not SHA.fullmatch(data_sha):
        raise HistoryImportError("untrusted normalized history checksum")
    identity = f"{info['source']}:{info['source_url']}:{data_sha}"
    batch = "external-research-v1-" + hashlib.sha256(
        identity.encode()).hexdigest()[:24]
    history = await drive._find_batch_object(
        parent_id=drive.folders.history, batch_id=batch, kind="history",
    )
    if (history is None or history.name != info["file"]
        or not isinstance(history.size_bytes, int)):
        raise HistoryImportError("remote historical candle original missing")
    raw = await _get_source(
        drive, file_id=history.file_id, expected_sha=data_sha,
        size=history.size_bytes, max_bytes=12_000_000,
    )
    scope = root / f"{asset}-{month}"
    scope.mkdir(parents=True, exist_ok=True)
    mpath = scope / name
    dpath = scope / info["file"]
    mpath.write_bytes(contents)
    dpath.write_bytes(raw)
    item = prepare(mpath)
    if item.history.sha256 != data_sha or item.manifest.sha256 != sha:
        raise HistoryImportError("remote original source changed during audit")
    grid = audit_partition(mpath)
    exact = (
        grid["venue"] == "binance" and grid["contract"] == asset + "USDT"
        and grid["month"] == month and grid["interval_minutes"] == 5
        and grid["full_calendar_month_verified"] is True
    )
    # A prior SHA-verified source URL is not a retained original provider ZIP.
    original_sha = info.get("source_sha256")
    original = None
    if isinstance(original_sha, str) and SHA.fullmatch(original_sha):
        original = await drive._find_batch_object(
            parent_id=drive.folders.history,
            batch_id="binance-original-" + original_sha[:24],
            kind="history",
        )
    original_readback = False
    if original is not None:
        expected = f"{asset}USDT-5m-{month}.zip"
        if original.name != expected:
            raise HistoryImportError("wrong original Binance ZIP identity")
        await _get_source(
            drive, file_id=original.file_id, expected_sha=original_sha,
            size=original.size_bytes, max_bytes=32 * 1024 * 1024,
        )
        original_readback = True
    return {
        "venue": "binance", "asset": asset, "month": month,
        "research_candles_sha256_readback": True,
        "exact_full_calendar_5m_verified": exact,
        "provider_original_zip_in_drive_readback": original_readback,
        "provider_original_zip_not_found": not original_readback,
        "provider_checksum_previous_capture_verified": True,
        "provider_checksum_bytes_readback_this_audit": False,
        "full_original_evidence_verified": False,
        "live_model_admitted": False,
    }


async def run(*, out: Path, max_attempts: int = 4,
              now: datetime | None = None) -> dict:
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    drive = _drive(Settings())
    try:
        await drive._verify_folder_boundary()
        selected = choose(await list_names(drive), now=clock, limit=max_attempts)
        with tempfile.TemporaryDirectory(prefix="rival-sha-audit-") as folder:
            evidence = []
            for name in selected:
                try:
                    evidence.append(await verify_month(
                        drive, name=name, root=Path(folder),
                    ))
                except (HistoryImportError, ValueError, OSError, KeyError) as exc:
                    evidence.append({
                        "manifest_name": name, "status": "unverified_or_invalid",
                        "reason": str(exc)[:180],
                        "calendar_complete_verified": False,
                    })
        report = {
            "schema_version": "rival-private-drive-original-byte-audit-v1",
            "observed_at": clock.isoformat(),
            "sampled_months": evidence,
            "months_calendar_byte_verified": sum(
                r.get("exact_full_calendar_5m_verified", False) for r in evidence),
            "original_zip_drive_sha_verified": sum(
                r.get("provider_original_zip_in_drive_readback", False)
                for r in evidence),
            "total_warehouse_history_verified": False,
            "all_ten_venue_sources_verified": False,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        artifact = _artifact(out, len(evidence) or 1)
        await drive.ensure_research_output(
            artifact=artifact, batch_id="historical-byte-audit-" + artifact.sha256[:24],
            kind="report",
        )
        return report
    finally:
        await drive.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path(
        "data/native-reports/drive-original-byte-audit.json"))
    args = parser.parse_args()
    result = asyncio.run(run(out=args.out, max_attempts=args.max_attempts))
    print(json.dumps({
        "audited": len(result["sampled_months"]),
        "exact_full_calendar_months": result["months_calendar_byte_verified"],
        "original_provider_ZIPs_read_back": result["original_zip_drive_sha_verified"],
        "all_history_complete": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
