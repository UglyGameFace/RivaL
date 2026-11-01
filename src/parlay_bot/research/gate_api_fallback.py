"""Bounded same-venue API recovery when a Gate monthly gzip is unavailable.

This is a separate research-only evidence path. It NEVER impersonates the
official gzip, never fills Gate with another venue, and never feeds models.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_bulk_archive import ASSETS
from parlay_bot.research.gate_native import (
    API,
    _request_json,
    candle,
    month_bounds,
    preserve_gate_api_response,
)
from parlay_bot.research.publish import _artifact

# 31 days contains at most 8,928 5-minute slots; ten bounded calls suffice.
POINTS_PER_REQUEST = 900
MAX_WINDOWS = 10


async def listed_api_fallback_months(drive) -> set[tuple[str, str]]:
    """Resume hints from previously archived Gate API-only evidence receipts.

    This metadata listing does not certify historical source-native training
    or prove the previous witness bytes were rehashed today. It only prevents
    spending 9-10 Gate requests again for the same existing research receipt.
    """
    await drive._verify_folder_boundary()
    folder = drive.folders.history
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        "and name contains 'gate-api-fallback-' "
        "and appProperties has { key='rival_kind' and value='history' }"
    )
    exact = re.compile(
        r"gate-api-fallback-(BTC|ETH|SOL|XRP)-"
        r"(20\d{2}-(?:0[1-9]|1[0-2]))-([0-9a-f]{16})\.json"
    )
    seen: dict[tuple[str, str], str] = {}
    page = None
    for _ in range(10):
        params = {
            "q": query, "spaces": "drive", "pageSize": 1000,
            "fields": "nextPageToken,files(name,parents,trashed,appProperties)",
        }
        if page:
            params["pageToken"] = page
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params,
            headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("Gate API-only Drive inventory unavailable")
        data = response.json()
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list) or len(files) > 1000:
            raise HistoryImportError("invalid Gate API-only Drive inventory")
        for item in files:
            if (not isinstance(item, dict) or item.get("trashed")
                or folder not in (item.get("parents") or [])):
                raise HistoryImportError("Gate API-only receipt outside Drive boundary")
            props = item.get("appProperties") or {}
            if not isinstance(props, dict) or props.get("rival_kind") != "history":
                raise HistoryImportError("invalid Gate API-only receipt ownership")
            filename = item.get("name")
            match = exact.fullmatch(filename) if isinstance(filename, str) else None
            if match is None:
                continue
            asset, month, short = match.groups()
            sha = props.get("rival_sha256")
            if (not isinstance(sha, str) or len(sha) != 64
                or any(ch not in "0123456789abcdef" for ch in sha)
                or sha[:16] != short):
                raise HistoryImportError("Gate API receipt metadata digest mismatch")
            key = (asset, month)
            if key in seen and seen[key] != sha:
                raise HistoryImportError("conflicting immutable Gate API-only receipts")
            seen[key] = sha
        page = data.get("nextPageToken")
        if page is None:
            return set(seen)
        if not isinstance(page, str) or not 1 <= len(page) <= 1000:
            raise HistoryImportError("invalid Gate API-only inventory cursor")
    raise HistoryImportError("Gate API-only receipt inventory exceeds page budget")


async def recover_unavailable_archive(
    drive, *, client: httpx.Client, asset: str, month: str,
    root: Path, now: datetime,
) -> dict:
    """Preserve complete original API responses with independent SHA readback.

    If Gate no longer serves old data, leave the month unqualified. Missing,
    duplicate, off-grid, or nonmatching candles fail closed. This receipt is
    NOT an accepted source-native training manifest.
    """
    if asset not in ASSETS or now.utcoffset() is None:
        raise HistoryImportError("unapproved Gate API month recovery scope")
    start, end = month_bounds(month)
    if end > now.astimezone(UTC):
        raise HistoryImportError("cannot recover an unfinished Gate month")
    ticks = range(int(start.timestamp()), int(end.timestamp()), 300)
    if (len(ticks) + POINTS_PER_REQUEST - 1) // POINTS_PER_REQUEST > MAX_WINDOWS:
        raise HistoryImportError("Gate API month exceeds bounded request budget")
    records: dict[int, dict] = {}
    witnessed: list[tuple[dict, bytes]] = []
    for offset in range(0, len(ticks), POINTS_PER_REQUEST):
        window = ticks[offset:offset + POINTS_PER_REQUEST]
        low, high = window[0], window[-1]
        response, digest, raw = _request_json(
            client, path="candlesticks", params={
                "contract": f"{asset}_USDT", "from": low,
                "to": high + 299, "interval": "5m", "timezone": "utc0",
            },
        )
        if not isinstance(response, list) or len(response) > len(window):
            raise HistoryImportError("Gate API full-month window incompatible")
        covered = set()
        for entry in response:
            if not isinstance(entry, dict) or {"t", "v", "c", "h", "l", "o"} - set(entry):
                raise HistoryImportError("Gate API full-month candle schema invalid")
            stamp, candle_row = candle(
                [str(entry[key]) for key in ("t", "v", "c", "h", "l", "o")],
                month=month,
            )
            if stamp < low or stamp > high or stamp in records:
                raise HistoryImportError("Gate API full-month timestamp conflict")
            records[stamp] = candle_row
            covered.add(stamp)
        if covered != set(window):
            raise HistoryImportError(
                f"Gate API historical coverage unavailable for {asset} {month} "
                f"window {offset // POINTS_PER_REQUEST + 1}"
            )
        witnessed.append(({
            "from": low, "to": high + 299, "response_sha256": digest,
            "records": len(window), "source_url": f"{API}/candlesticks",
        }, raw))

    # Only now, after every source window passes, persist the actual responses.
    proofs = []
    for info, raw in witnessed:
        info["raw_json_file"] = await preserve_gate_api_response(
            drive, root=root, payload=raw, sha256=info["response_sha256"],
        )
        proofs.append(info)
    receipt = {
        "schema_version": "rival-gate-api-fallback-evidence-v1",
        "venue": "gate", "contract": f"{asset}_USDT",
        "asset": asset, "month": month, "interval_minutes": 5,
        "source_transport": "official_gate_v4_public_api",
        "original_provider_archive_available": False,
        "source_api_response_readback_verified": True,
        "calendar_5m_complete": len(records) == len(ticks),
        "candles": len(records), "windows": proofs,
        "model_training_eligible": False, "live_model_promoted": False,
        "reason_not_training_eligible":
            "API evidence requires separately qualified historical contract settings "
            "and a source-native importer; cannot impersonate the original gzip",
    }
    serialized = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    name_hash = hashlib.sha256(serialized).hexdigest()
    path = root / "gate-api-evidence" / (
        f"gate-api-fallback-{asset}-{month}-{name_hash[:16]}.json"
    )
    if path.exists() and path.read_bytes() != serialized:
        raise HistoryImportError("immutable Gate API fallback receipt changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(serialized)
    artifact = _artifact(path, 1)
    stored = await drive.ensure_history_file(
        artifact=artifact, batch_id="gateapi-" + artifact.sha256[:24],
        mime_type="application/json",
    )
    await drive.verify_downloaded_artifact(
        file_id=stored.file_id, parent_id=drive.folders.history,
        artifact=artifact,
    )
    return {
        "asset": asset, "month": month, "status": "api_complete_research_only",
        "candles": len(records), "api_windows": len(proofs),
        "receipt_sha256": artifact.sha256,
        "source_api_response_readback_verified": True,
        "model_training_eligible": False,
    }
