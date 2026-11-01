"""Gate futures 5m source qualification, genuine gap investigation, offline-only research.

Original Gate gzip remains immutable in Drive. Missing slots can only be
recovered from the *same* Gate USDT perpetual via the documented public API,
with a separate exact response SHA and timestamp provenance. Gate's OHLCV
volume is contract units: historical base units are only derived when the
official contract's last configuration update predates the source month.

A successfully qualified source month is a *retrospective research input*,
not a new live forecast or an approved production model.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.research.audit import audit_partition
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.gate_bulk_archive import (
    ASSETS,
    MAX_BYTES,
    _finite,
    _validate_row,
    inspect,
    url_for,
)
from parlay_bot.research.publish import _artifact
from parlay_bot.research.source_native import SOURCE

API = "https://api.gateio.ws/api/v4/futures/usdt"
INTEGRITY = "gate-official-gzip-with-v4-api-reconciliation-v1"
STEP = timedelta(minutes=5)
MAX_GAP_REQUESTS = 10
MAX_GAP_POINTS_PER_REQUEST = 1500
MAX_GAP_SLOTS = 96
MAX_API_BYTES = 512 * 1024


def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise HistoryImportError("invalid Gate UTC source month") from exc
    if first.strftime("%Y-%m") != month:
        raise HistoryImportError("noncanonical Gate source month")
    next_month = (first.replace(year=first.year + 1, month=1)
                  if first.month == 12 else first.replace(month=first.month + 1))
    return first, next_month


def candle(raw: list[str], *, month: str) -> tuple[int, dict]:
    if len(raw) != 6:
        raise HistoryImportError("Gate original candle has incompatible columns")
    timestamp = _validate_row(raw, kind="candlesticks_5m", month=month)
    if not timestamp.is_integer() or int(timestamp) % 300:
        raise HistoryImportError("Gate original candle off five-minute UTC grid")
    values = [_finite(v) for v in raw[1:]]
    volume, close, high, low, opened = values
    if volume < 0:
        raise HistoryImportError("Gate contract volume negative")
    return int(timestamp), {
        "volume_contracts": volume,
        "open": opened, "high": high, "low": low, "close": close,
    }


def original_candles(archive: Path, *, asset: str, month: str,
                     expected_sha: str) -> tuple[dict[int, dict], dict]:
    if asset not in ASSETS or not archive.is_file() or archive.stat().st_size > MAX_BYTES:
        raise HistoryImportError("untrusted or oversized Gate raw archive")
    payload = archive.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha:
        raise HistoryImportError("original Gate archive changed since raw Drive proof")
    quality = inspect(payload, asset=asset, kind="candlesticks_5m", month=month)
    if digest != quality["compressed_sha256"]:
        raise HistoryImportError("Gate compressed byte identity mismatch")
    records: dict[int, dict] = {}
    with gzip.open(archive, "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            # inspect() already verified source structure and optional header.
            if quality["has_header"] and tuple(v.strip().lower() for v in row) == (
                "timestamp", "size", "close", "high", "low", "open",
            ):
                continue
            timestamp, data = candle(row, month=month)
            if timestamp in records:
                raise HistoryImportError("duplicate Gate archive candle")
            records[timestamp] = data
    if len(records) != quality["rows"]:
        raise HistoryImportError("Gate archive changed during source normalization")
    start, stop = month_bounds(month)
    low, high = int(start.timestamp()), int(stop.timestamp())
    if any(stamp < low or stamp >= high for stamp in records):
        raise HistoryImportError("source archive contains out-of-UTC-month candles")
    return records, quality


def gaps(records: dict[int, dict], *, month: str) -> list[int]:
    first, stop = month_bounds(month)
    expected = set(range(int(first.timestamp()), int(stop.timestamp()), 300))
    if not set(records) <= expected:
        raise HistoryImportError("Gate candle archive has off-calendar timestamps")
    return sorted(expected - set(records))


def _request_json(client: httpx.Client, *, path: str,
                  params: dict | None = None) -> tuple[object, str]:
    if path not in {"candlesticks"} and not path.startswith("contracts/"):
        raise HistoryImportError("unapproved Gate market data endpoint")
    try:
        response = client.get(f"{API}/{path}", params=params, timeout=35,
                              follow_redirects=False,
                              headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise HistoryImportError("Gate official API currently unavailable") from exc
    if response.status_code != 200 or response.is_redirect:
        raise HistoryImportError(f"Gate gap API rejected HTTP {response.status_code}")
    if not 0 < len(response.content) <= MAX_API_BYTES:
        raise HistoryImportError("Gate public API response exceeds budget")
    try:
        return response.json(), hashlib.sha256(response.content).hexdigest()
    except ValueError as exc:
        raise HistoryImportError("invalid Gate API source JSON") from exc


def source_multiplier(client: httpx.Client, *, asset: str,
                      month: str) -> dict:
    if asset not in ASSETS:
        raise HistoryImportError("unapproved Gate contract multiplier asset")
    info, digest = _request_json(client, path=f"contracts/{asset}_USDT")
    first, _ = month_bounds(month)
    if not isinstance(info, dict) or info.get("name") != f"{asset}_USDT":
        raise HistoryImportError("Gate contract multiplier identity unverified")
    if info.get("type") != "direct":
        raise HistoryImportError("Gate historical contract is not verified linear")
    multiplier = _finite(info.get("quanto_multiplier"), allow_negative=False)
    if multiplier <= 0:
        raise HistoryImportError("Gate historical contract multiplier unavailable")
    try:
        configured = int(info["config_change_time"])
        created = int(info["create_time"])
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise HistoryImportError("Gate has no dated contract-size configuration") from exc
    if not 0 < created <= configured <= int(first.timestamp()):
        raise HistoryImportError(
            "Gate contract multiplier changed after the historical source month"
        )
    return {
        "multiplier": multiplier,
        "contract_created_at_unix": created,
        "contract_config_last_changed_unix": configured,
        "contract_metadata_sha256": digest,
        "contract_metadata_url": f"{API}/contracts/{asset}_USDT",
    }


def investigate_missing(client: httpx.Client, *, asset: str,
                        month: str, missing: list[int]) -> tuple[dict[int, dict], list]:
    if not missing:
        return {}, []
    if not 0 < len(missing) <= MAX_GAP_SLOTS:
        raise HistoryImportError("Gate source gaps exceed bounded API investigation")
    first, stop = month_bounds(month)
    expected_start = int(first.timestamp())
    expected_end = int(stop.timestamp())
    recovered: dict[int, dict] = {}
    witnesses: list[dict] = []
    pending = sorted(set(missing))
    if len(pending) != len(missing):
        raise HistoryImportError("duplicate Gate missing slot requests")
    while pending:
        if len(witnesses) >= MAX_GAP_REQUESTS:
            raise HistoryImportError("Gate missing slots exceed per-month request budget")
        start = pending[0]
        selection = [stamp for stamp in pending
                     if stamp <= start + 300 * (MAX_GAP_POINTS_PER_REQUEST - 1)]
        end = selection[-1]
        response, digest = _request_json(client, path="candlesticks", params={
            "contract": f"{asset}_USDT", "from": start, "to": end + 299,
            "interval": "5m", "timezone": "utc0",
        })
        if not isinstance(response, list) or len(response) > MAX_GAP_POINTS_PER_REQUEST:
            raise HistoryImportError("invalid Gate API gap window response")
        captured = []
        for entry in response:
            if not isinstance(entry, dict) or {"t", "v", "c", "h", "l", "o"} - set(entry):
                raise HistoryImportError("Gate API returned incompatible candle")
            raw = [str(entry[key]) for key in ("t", "v", "c", "h", "l", "o")]
            stamp, row = candle(raw, month=month)
            if (stamp < expected_start or stamp >= expected_end or
                stamp < start or stamp > end):
                raise HistoryImportError("Gate API candle outside requested gap range")
            if stamp in recovered:
                raise HistoryImportError("Gate API returned duplicate gap candle")
            if stamp in missing:
                recovered[stamp] = row
                captured.append(stamp)
        witnesses.append({
            "from": start, "to": end + 299,
            "response_sha256": digest, "recovered": sorted(captured),
            "source_url": f"{API}/candlesticks",
        })
        pending = [stamp for stamp in pending if stamp not in selection]
    return recovered, witnesses


async def qualify_and_publish(
    drive, *, client: httpx.Client, archive: Path, source: dict,
    asset: str, month: str, root: Path, now: datetime,
) -> dict:
    """Publish only complete Gate OHLCV with dated contract-size provenance.

    Raw archive must already have passed verified Drive readback.
    Recovered candles remain individually documented as Gate API, never as
    the original gzip. All publication stays retrospective research-only.
    """
    if (now.utcoffset() is None or month_bounds(month)[1] > now.astimezone(UTC)
        or source.get("kind") != "candlesticks_5m"
        or source.get("asset") != asset or source.get("month") != month
        or source.get("source_url") !=
            url_for(asset=asset, kind="candlesticks_5m", month=month)):
        raise HistoryImportError("unverified Gate qualification input identity")
    raw_sha = source.get("compressed_sha256")
    if not isinstance(raw_sha, str) or len(raw_sha) != 64:
        raise HistoryImportError("unverified Gate source digest")
    original, raw_quality = original_candles(
        archive, asset=asset, month=month, expected_sha=raw_sha,
    )
    absent = gaps(original, month=month)
    recovered, witnesses = investigate_missing(
        client, asset=asset, month=month, missing=absent,
    )
    if set(recovered) - set(absent):
        raise HistoryImportError("Gate API attempted to overwrite source archive")
    complete = {**original, **recovered}
    if gaps(complete, month=month):
        raise HistoryImportError("Gate API did not recover every missing source candle")
    evidence = source_multiplier(client, asset=asset, month=month)
    volume_factor = evidence["multiplier"]
    stamp = now.astimezone(UTC)
    records = []
    for t in sorted(complete):
        opened = datetime.fromtimestamp(t, UTC)
        row = complete[t]
        base_volume = row["volume_contracts"] * volume_factor
        if not math.isfinite(base_volume) or base_volume < 0:
            raise HistoryImportError("Gate source base volume cannot be represented")
        records.append({
            "venue": "gate", "contract": f"{asset}_USDT", "quote": "USDT",
            "interval_minutes": 5,
            "opened_at": opened.isoformat(),
            "closed_at": (opened + STEP - timedelta(milliseconds=1)).isoformat(),
            "first_observed_at": stamp.isoformat(),
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "volume_contracts": row["volume_contracts"],
            "volume_base": base_volume, "trades": -1,
        })
    from parlay_bot.research.gate_archive_backfill import stem
    raw_id = stem(asset=asset, kind="candlesticks_5m", month=month, sha=raw_sha)
    normalized = _persist(
        source=SOURCE, key=f"gate-{asset}_USDT-USDT-5m-{month}",
        records=records,
        raw_sha=raw_sha, url=source["source_url"],
        out_dir=root,
        extra={
            "venue": "gate", "contract": f"{asset}_USDT", "quote": "USDT",
            "interval_minutes": 5,
            "checksum_verified": True,
            "provider_signed_checksum": False,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_transport": "official_public_https",
            "source_integrity": INTEGRITY,
            "source_archive_type": "five_minute_monthly_csv_gz",
            "source_archive_sha256": raw_sha,
            "source_archive_file": raw_id + ".csv.gz",
            "source_archive_rows": raw_quality["rows"],
            "source_minutes_verified": len(records) * 5,
            "source_api_recovered_count": len(recovered),
            "source_api_recovery": witnesses,
            "volume_base_conversion": "contract_size_times_historically_dated_quanto_multiplier",
            "contract_multiplier_evidence": evidence,
            "original_observation_time_known": False,
            "historical_source_complete_utc_month": True,
        },
    )
    audit = audit_partition(normalized.manifest_path)
    if (audit["venue"] != "gate" or audit["contract"] != f"{asset}_USDT"
        or audit["month"] != month or not audit["full_calendar_month_verified"]
        or audit["rows"] != len(records)):
        raise HistoryImportError("Gate normalized source month failed exact continuity")
    data = _artifact(normalized.data_path, normalized.records)
    manifest = _artifact(normalized.manifest_path, 1)
    batch_id = "native-" + data.sha256[:24]
    saved_data = await drive.ensure_history_file(
        artifact=data, batch_id=batch_id, mime_type="application/x-ndjson",
    )
    await drive.verify_downloaded_artifact(
        file_id=saved_data.file_id, parent_id=drive.folders.history, artifact=data,
    )
    saved_manifest = await drive.ensure_manifest_file(
        artifact=manifest, batch_id=batch_id,
    )
    await drive.verify_downloaded_artifact(
        file_id=saved_manifest.file_id, parent_id=drive.folders.manifests,
        artifact=manifest,
    )
    return {
        "asset": asset, "month": month,
        "status": "verified_native_research_only",
        "total_candles": len(records), "raw_source_candles": len(original),
        "same_venue_api_recovered": len(recovered),
        "source_raw_sha256": raw_sha,
        "normalized_sha256": data.sha256,
        "source_venue": "gate",
        "source_training_eligible": True,
        "forward_skill_proven": False,
        "live_model_promoted": False,
        "drive_readback_sha256_verified": True,
    }
