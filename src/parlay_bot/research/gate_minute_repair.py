"""Repair evidence for missing Gate five-minute bars from original Gate one-minute CSV.

The original provider five-minute and one-minute gzips stay separate and
immutable. A derived candle is a documented reconstruction from five actual,
continuous one-minute observations of the *same contract*, never interpolation
or another exchange's data. Contract-sized volume is not converted to base
units without a dated historic Gate multiplier. This pipeline does not train.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_bulk_archive import (
    ASSETS,
    MAX_BYTES,
    _finite,
    _validate_row,
    download,
    inspect,
    url_for,
)
from parlay_bot.research.gate_native import gaps, month_bounds
from parlay_bot.research.publish import _artifact

MAX_REPAIRS_PER_MONTH = 128
MAX_CHECKS = 64


def minute_rows(raw_path: Path, *, asset: str, month: str,
                expected_sha: str) -> tuple[dict[int, dict], dict]:
    if (asset not in ASSETS or not raw_path.is_file()
        or raw_path.stat().st_size > MAX_BYTES):
        raise HistoryImportError("untrusted Gate original one-minute archive")
    data = raw_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha:
        raise HistoryImportError("Gate one-minute original checksum diverged")
    quality = inspect(data, asset=asset, kind="candlesticks_1m", month=month)
    start, end = month_bounds(month)
    low, high = int(start.timestamp()), int(end.timestamp())
    records: dict[int, dict] = {}
    try:
        with gzip.open(raw_path, "rt", encoding="utf-8-sig", newline="") as stream:
            for raw in csv.reader(stream):
                if quality["has_header"] and tuple(
                    val.strip().lower() for val in raw
                ) == ("timestamp", "size", "close", "high", "low", "open"):
                    continue
                stamp = _validate_row(raw, kind="candlesticks_1m", month=month)
                if (not stamp.is_integer() or int(stamp) % 60
                    or not low <= int(stamp) < high):
                    raise HistoryImportError("Gate minute record outside exact UTC grid")
                ts = int(stamp)
                if ts in records:
                    raise HistoryImportError("duplicate Gate one-minute candle")
                size, close, highest, lowest, opened = (
                    _finite(raw[i], allow_negative=False) for i in range(1, 6)
                )
                records[ts] = {
                    "open": opened, "high": highest, "low": lowest,
                    "close": close, "volume_contracts": size,
                }
    except (OSError, UnicodeError, csv.Error) as exc:
        raise HistoryImportError("unreadable Gate one-minute archive") from exc
    if len(records) != quality["rows"]:
        raise HistoryImportError("Gate minute archive rows changed after inspection")
    return records, quality


def five_from_minutes(minutes: dict[int, dict], tick: int) -> dict | None:
    series = [minutes.get(tick + n * 60) for n in range(5)]
    if any(x is None for x in series):
        return None
    group = [x for x in series if x is not None]
    size = math.fsum(x["volume_contracts"] for x in group)
    if not math.isfinite(size) or size < 0:
        raise HistoryImportError("Gate one-minute contract volume not representable")
    return {
        "open": group[0]["open"], "close": group[-1]["close"],
        "high": max(x["high"] for x in group),
        "low": min(x["low"] for x in group),
        "volume_contracts": size,
    }


def reconstruct_gaps(
    *, original_5m: dict[int, dict], one_minute: dict[int, dict],
    asset: str, month: str, original_sha: str, minute_sha: str,
) -> tuple[dict[int, dict], dict]:
    if (asset not in ASSETS or len(original_sha) != 64 or len(minute_sha) != 64
        or any(c not in "0123456789abcdef" for c in original_sha + minute_sha)):
        raise HistoryImportError("untrusted Gate source repair identity")
    missing = gaps(original_5m, month=month)
    if not missing or len(missing) > MAX_REPAIRS_PER_MONTH:
        raise HistoryImportError("Gate source missing slot scope unavailable")
    # Before allowing even one recovery, establish that one-minute and
    # five-minute provider archives represent the same price and volume.
    eligible = [
        ts for ts in sorted(original_5m)
        if five_from_minutes(one_minute, ts) is not None
    ]
    if len(eligible) < 4:
        raise HistoryImportError("insufficient same-source 1m/5m crosschecks")
    stride = max(1, len(eligible) // MAX_CHECKS)
    sampled = eligible[::stride][:MAX_CHECKS]
    for ts in sampled:
        expected = original_5m[ts]
        actual = five_from_minutes(one_minute, ts)
        if actual is None:
            raise HistoryImportError("Gate one-minute comparison unexpectedly missing")
        for name in ("open", "high", "low", "close", "volume_contracts"):
            if (not math.isfinite(float(expected[name]))
                or not math.isclose(float(expected[name]), actual[name],
                                    rel_tol=1e-8, abs_tol=1e-9)):
                raise HistoryImportError(
                    "Gate one-minute archive disagrees with original five-minute candles"
                )
    repaired = {}
    for ts in missing:
        actual = five_from_minutes(one_minute, ts)
        if actual is not None:
            repaired[ts] = actual
    remainder = sorted(set(missing) - set(repaired))
    evidence = {
        "schema_version": "rival-gate-original-1m-gap-evidence-v1",
        "venue": "gate", "contract": f"{asset}_USDT", "quote": "USDT",
        "month": month, "source_original_5m_sha256": original_sha,
        "source_original_1m_sha256": minute_sha,
        "source_original_1m_url": url_for(
            asset=asset, kind="candlesticks_1m", month=month,
        ),
        "missing_source_5m_before": len(missing),
        "five_minute_candles_reconstructed_from_exact_5x_1m": len(repaired),
        "crosschecked_5m_source_bars": len(sampled),
        "reconstructed_open_unix_seconds": sorted(repaired),
        "unresolved_open_unix_seconds": remainder,
        "source_1m_reconstructions": [
            {"opened_unix_seconds": ts,
             "actual_1m_source_timestamps": [ts + j * 60 for j in range(5)],
             "contract_ohlcv": repaired[ts]}
            for ts in sorted(repaired)
        ],
        "live_model_promoted": False,
        "model_training_eligible": False,
        "historical_contract_multiplier_verified": False,
        "not_interpolated": True,
        "source_provider_signed_checksum": False,
    }
    return repaired, evidence


async def publish_minute_gap_evidence(
    drive, *, client: httpx.Client, original_5m: dict[int, dict],
    original_5m_sha: str, asset: str, month: str, root: Path,
) -> dict:
    """Download one bounded real Gate minute archive and SHA-store evidence."""
    if asset not in ASSETS or not original_5m_sha:
        raise HistoryImportError("untrusted Gate minute repair scope")
    from parlay_bot.research.gate_archive_backfill import publish_raw

    output = root / "incoming" / "candlesticks_1m" / month / (
        f"{asset}_USDT-{month}.csv.gz"
    )
    source = download(client=client, asset=asset, kind="candlesticks_1m",
                      month=month, output=output)
    one_minute, _ = minute_rows(
        output, asset=asset, month=month,
        expected_sha=source["compressed_sha256"],
    )
    repaired, details = reconstruct_gaps(
        original_5m=original_5m, one_minute=one_minute,
        asset=asset, month=month, original_sha=original_5m_sha,
        minute_sha=source["compressed_sha256"],
    )
    # Preserve even when the 1m source was incomplete: the genuine recovered
    # slots are evidence, never an invented complete native 5m month.
    saved = await publish_raw(
        drive, archive=output, source=source, asset=asset,
        kind="candlesticks_1m", month=month, root=root,
    )
    if saved.get("warehouse_readback_sha256_verified") is not True:
        raise HistoryImportError("Gate raw one-minute Drive copy unverified")
    from parlay_bot.research.gate_archive_backfill import stem

    name = stem(
        asset=asset, kind="candlesticks_1m", month=month,
        sha=source["compressed_sha256"],
    ) + ".csv.gz"
    details["source_original_1m_file"] = name
    path = root / "gate-raw-verified" / (
        f"gate-minute-gap-{asset}-{month}-{source['compressed_sha256'][:16]}.json"
    )
    encoded = (json.dumps(details, sort_keys=True, indent=2) + "\n").encode()
    if path.exists() and path.read_bytes() != encoded:
        raise HistoryImportError("immutable Gate 1m repair report mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(encoded)
    artifact = _artifact(path, 1)
    research = await drive.ensure_research_output(
        artifact=artifact, batch_id="gate-minute-" + artifact.sha256[:24],
        kind="report",
    )
    await drive.verify_downloaded_artifact(
        file_id=research.file_id, parent_id=drive.folders.reports,
        artifact=artifact,
    )
    return {
        "asset": asset, "month": month,
        "source": "official_gate_one_minute_archive",
        "raw_1m_sha256": source["compressed_sha256"],
        "original_5m_sha256": original_5m_sha,
        "source_5m_missing": details["missing_source_5m_before"],
        "same_gate_1m_candles_derived": len(repaired),
        "still_missing": len(details["unresolved_open_unix_seconds"]),
        "crosschecked_existing_5m": details["crosschecked_5m_source_bars"],
        "original_minute_drive_sha_readback_verified": True,
        "repair_receipt_drive_sha_readback_verified": True,
        "model_training_eligible": False,
        "report_sha256": artifact.sha256,
    }
