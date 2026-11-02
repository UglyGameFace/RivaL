"""Restore Bitget's ORIGINAL 5m API months to offline research, never live trading.

The collector owns raw immutable response archives. This importer only derives a
separate, source-labeled research view after *fresh* private Drive byte readback.
Premium/index/mark price bars are intentionally NOT treated as trade prices.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.research.bitget_v3_archive import (
    ASSETS, ENDPOINT, METADATA, PAGE, STEP, bounds, validate_response, windows,
)
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.source_native import SOURCE

_NAME = re.compile(
    r"bitget-v3-(BTC|ETH|SOL|XRP)-market-(20\d\d-(?:0[1-9]|1[0-2]))--"
    r"([0-9a-f]{16})\.manifest\.json"
)
_SHA = re.compile(r"[0-9a-f]{64}")
_MAX_RAW = 3 * 1024 * 1024
_MAX_DECODED = 16 * 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _download(drive, *, item, expected_sha: str, max_bytes: int) -> bytes:
    if (not _SHA.fullmatch(expected_sha)
        or not isinstance(item.size_bytes, int)
        or not 0 < item.size_bytes <= max_bytes):
        raise HistoryImportError("Bitget Drive object identity/size invalid")
    response = await drive._client.get(
        f"{drive.DRIVE_URL}/files/{item.file_id}",
        params={"alt": "media"}, headers=await drive._headers(),
        follow_redirects=False,
    )
    if (response.status_code != 200
        or len(response.content) != item.size_bytes
        or _sha(response.content) != expected_sha):
        raise HistoryImportError("Bitget Drive SHA-256 byte readback failed")
    return response.content


def _source_month(raw: bytes, manifest: dict, *, asset: str, month: str) -> list[dict]:
    """Parse every original API response and prove the entire exact UTC grid."""
    first, last = bounds(month)
    pages = windows(month)
    if (manifest.get("schema_version") != "rival-bitget-v3-raw-api-month-v1"
        or manifest.get("venue") != "bitget"
        or manifest.get("asset") != asset
        or manifest.get("contract") != asset + "USDT"
        or manifest.get("quote") != "USDT"
        or manifest.get("family") != "market"
        or manifest.get("category") != "USDT-FUTURES"
        or manifest.get("market_type") != "usdt_linear_perpetual"
        or manifest.get("interval_minutes") != 5
        or manifest.get("utc_month") != month
        or manifest.get("source_url") != ENDPOINT
        or manifest.get("source_transport") != "bitget_public_v3_api"
        or manifest.get("exact_utc_5m_grid_verified") is not True
        or manifest.get("provider_signed_checksum") is not False
        or manifest.get("historical_listing_date_verified") is not False
        or manifest.get("retrospective") is not True
        or manifest.get("prospective_forecast") is not False
        or manifest.get("live_trading_authorized") is not False
        or manifest.get("model_training_eligible") is not False
        or manifest.get("request_pages") != len(pages)
        or manifest.get("candles") != (last - first) // STEP
        or not isinstance(manifest.get("source_metadata_sha256"), str)
        or not _SHA.fullmatch(manifest["source_metadata_sha256"])
        or not isinstance(manifest.get("concatenated_original_response_sha256"), str)
        or not _SHA.fullmatch(manifest["concatenated_original_response_sha256"])):
        raise HistoryImportError("Bitget market month provenance incompatible")
    try:
        observed = datetime.fromisoformat(manifest["first_observed_at"])
    except (ValueError, TypeError, KeyError) as exc:
        raise HistoryImportError("Bitget original observation time invalid") from exc
    if (observed.utcoffset() is None
        or observed.astimezone(UTC) < datetime.fromtimestamp(last / 1000, UTC)
        or observed.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=5)):
        raise HistoryImportError("Bitget source observation clock impossible")

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            expanded = stream.read(_MAX_DECODED + 1)
        if len(expanded) > _MAX_DECODED:
            raise HistoryImportError("Bitget raw source decompression budget exceeded")
    except (OSError, EOFError, ValueError) as exc:
        raise HistoryImportError("Bitget raw source archive corrupt") from exc
    lines = expanded.splitlines()
    if len(lines) != len(pages):
        raise HistoryImportError("Bitget original API page count incomplete")
    seen: dict[int, list] = {}
    combined = hashlib.sha256()
    for line, (begin, end) in zip(lines, pages, strict=True):
        if len(line) > 250_000:
            raise HistoryImportError("Bitget original response page oversized")
        try:
            item = json.loads(line)
            request = item["request"]
            response_bytes = base64.b64decode(item["response_base64"], validate=True)
        except (ValueError, KeyError, TypeError) as exc:
            raise HistoryImportError("Bitget source response encoding invalid") from exc
        if (not isinstance(item, dict) or not isinstance(request, dict)
            or request != {
                "endpoint": ENDPOINT, "category": "USDT-FUTURES",
                "symbol": asset + "USDT", "type": "market",
                "interval": "5m", "startTime": begin - 1,
                "endTime": end + STEP - 1, "limit": 100,
            }
            or not isinstance(item.get("response_sha256"), str)
            or _sha(response_bytes) != item["response_sha256"]):
            raise HistoryImportError("Bitget original request or response hash mismatch")
        combined.update(response_bytes)
        page_rows = validate_response(
            response_bytes, start=begin, end=end, family="market",
        )
        expected = (end - begin) // STEP
        if len(page_rows) != expected or set(page_rows) != set(range(begin, end, STEP)):
            raise HistoryImportError("Bitget original market page has missing UTC candles")
        if any(t in seen for t in page_rows):
            raise HistoryImportError("Bitget original source duplicated a candle")
        seen.update(page_rows)
    if (combined.hexdigest() != manifest["concatenated_original_response_sha256"]
        or set(seen) != set(range(first, last, STEP))):
        raise HistoryImportError("Bitget original source month integrity mismatch")

    rows = []
    for tick in range(first, last, STEP):
        fields = seen[tick]
        opened = datetime.fromtimestamp(tick / 1000, UTC)
        closed = datetime.fromtimestamp((tick + STEP - 1) / 1000, UTC)
        rows.append({
            "venue": "bitget", "contract": asset + "USDT", "quote": "USDT",
            "interval_minutes": 5, "opened_at": opened.isoformat(),
            "closed_at": closed.isoformat(),
            "first_observed_at": observed.isoformat(),
            "open": float(fields[1]), "high": float(fields[2]),
            "low": float(fields[3]), "close": float(fields[4]),
            "volume_base": float(fields[5]),
            # V3 market response reports volume, but NOT actual trade count.
            "trades": -1,
        })
    return rows


async def restore_verified_bitget_market_months(
    drive, *, months: list[str], root: Path, asset: str,
) -> list[Path]:
    """Read only genuine source market months, verifying Drive and source hashes.

    One asset per run bounds training memory and Drive transfers; all four
    continue through scheduled rotation. Missing months remain missing.
    """
    if asset not in ASSETS or not 1 <= len(months) <= 6 or len(set(months)) != len(months):
        raise HistoryImportError("invalid bounded Bitget research scope")
    for month in months:
        bounds(month)
    await drive._verify_folder_boundary()
    found: dict[str, tuple[object, str]] = {}
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        f"and name contains 'bitget-v3-{asset}-market-' "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    token = None
    for _ in range(10):
        params = {
            "q": query, "spaces": "drive", "pageSize": 100,
            "fields": "nextPageToken,files(id,name,size,parents,trashed,appProperties)",
        }
        if token:
            params["pageToken"] = token
        result = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params,
            headers=await drive._headers(), follow_redirects=False,
        )
        if result.status_code != 200:
            raise HistoryImportError("Bitget market manifest inventory unavailable")
        data = result.json()
        entries = data.get("files") if isinstance(data, dict) else None
        if not isinstance(entries, list) or len(entries) > 100:
            raise HistoryImportError("Bitget manifest inventory malformed")
        for entry in entries:
            if not isinstance(entry, dict):
                raise HistoryImportError("Bitget manifest listing malformed")
            match = _NAME.fullmatch(str(entry.get("name", "")))
            if not match or match[1] != asset or match[2] not in months:
                continue
            props = entry.get("appProperties") or {}
            if (entry.get("trashed") or folder not in (entry.get("parents") or [])
                or not isinstance(props, dict)
                or props.get("rival_kind") != "manifest"
                or not isinstance(props.get("rival_sha256"), str)
                or not _SHA.fullmatch(props["rival_sha256"])):
                raise HistoryImportError("Bitget manifest metadata invalid")
            if match[2] in found:
                raise HistoryImportError("duplicate Bitget original source month")
            found[match[2]] = (drive._object(entry), props["rival_sha256"])
        token = data.get("nextPageToken")
        if token is None:
            break
        if not isinstance(token, str) or not 0 < len(token) <= 1000:
            raise HistoryImportError("Bitget Drive page token invalid")
    else:
        raise HistoryImportError("Bitget manifest inventory exceeded bounded pages")

    imported = []
    for month in months:
        if month not in found:
            continue
        item, manifest_hash = found[month]
        manifest_bytes = await _download(
            drive, item=item, expected_sha=manifest_hash, max_bytes=8192,
        )
        try:
            info = json.loads(manifest_bytes)
        except (ValueError, TypeError) as exc:
            raise HistoryImportError("Bitget original manifest malformed") from exc
        if not isinstance(info, dict):
            raise HistoryImportError("Bitget original manifest must be an object")
        raw_hash = info.get("raw_sha256")
        if not isinstance(raw_hash, str) or not _SHA.fullmatch(raw_hash):
            raise HistoryImportError("Bitget original archive hash invalid")
        original_name = f"bitget-v3-{asset}-market-{month}--{raw_hash[:16]}"
        if (item.name != original_name + ".manifest.json"
            or info.get("raw_file") != original_name + ".responses.jsonl.gz"):
            raise HistoryImportError("Bitget original manifest/archive identity mismatch")
        archive = await drive._find_batch_object(
            parent_id=drive.folders.history, batch_id="bitgetv3-" + raw_hash[:24],
            kind="history",
        )
        if archive is None or archive.name != info["raw_file"]:
            raise HistoryImportError("Bitget original raw API archive missing")
        raw = await _download(
            drive, item=archive, expected_sha=raw_hash, max_bytes=_MAX_RAW,
        )
        candles = _source_month(raw, info, asset=asset, month=month)
        result = _persist(
            source=SOURCE, key=f"bitget-{asset}USDT-USDT-5m-{month}",
            records=candles, raw_sha=raw_hash, url=ENDPOINT,
            out_dir=root, extra={
                "venue": "bitget", "contract": asset + "USDT", "quote": "USDT",
                "interval_minutes": 5, "checksum_verified": True,
                "time_reference": "exchange_candle_open_not_first_observed_at",
                "source_integrity": "bitget-v3-original-api-5m-v1",
                "provider_signed_checksum": False,
                "source_transport": "bitget_public_v3_api",
                "source_archive_file": original_name + ".responses.jsonl.gz",
                "source_archive_sha256": raw_hash,
                "source_manifest_sha256": manifest_hash,
                "source_metadata_sha256": info["source_metadata_sha256"],
                "source_original_drive_readback_checked": True,
                "original_observation_time_known": False,
                "trades_observed": False,
                "historical_listing_date_verified": False,
                "model_training_eligible": False,
            },
        )
        imported.append(result.manifest_path)
    return imported
