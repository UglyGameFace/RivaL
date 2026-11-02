"""Bounded original Gate perpetual hourly L2 archive samples, research-only.

Gate explicitly publishes hourly orderbooks and orderbooks_slice gzips, starting
at an August 2021 *publication-era attempt floor*. This module captures raw
source bytes, validates their gzip envelope, and checks private Drive SHA.
It does NOT claim complete L2 replay, actual individual listing dates,
or historical quote availability for uncollected hours.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

BASE = "https://download.gatedata.org/futures_usdt"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
FAMILIES = ("orderbooks", "orderbooks_slice")
FIRST_ATTEMPT = datetime(2021, 8, 1, tzinfo=UTC)
MAX_COMPRESSED = 24 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
MAX_SOURCE_LINES = 2_000_000


def hour_bounds(hour: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    if (not isinstance(hour, str) or len(hour) != 10
        or not hour.isascii() or not hour.isdecimal()):
        raise HistoryImportError("invalid Gate L2 UTC hour YYYYMMDDHH")
    try:
        start = datetime.strptime(hour, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid Gate L2 calendar hour") from exc
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    end = start + timedelta(hours=1)
    if start < FIRST_ATTEMPT or end > clock:
        raise HistoryImportError("Gate L2 hour before archive era or not completed")
    return start, end


def source_url(*, asset: str, family: str, hour: str,
               now: datetime | None = None) -> str:
    if asset not in ASSETS or family not in FAMILIES:
        raise HistoryImportError("untrusted Gate L2 venue/contract/family")
    start, _ = hour_bounds(hour, now=now)
    suffix = ".gz" if family == "orderbooks_slice" else ".csv.gz"
    return (
        f"{BASE}/{family}/{start:%Y%m}/{asset}_USDT-{hour}{suffix}"
    )


def inspect_gzip(data: bytes) -> dict:
    """Bounded streaming gzip integrity check; not an L2 source-schema claim."""
    if not 0 < len(data) <= MAX_COMPRESSED or not data.startswith(b"\\x1f\\x8b"):
        raise HistoryImportError("untrusted or oversized Gate depth gzip")
    lines, expanded = 0, 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            while chunk := stream.read(64 * 1024):
                expanded += len(chunk)
                if expanded > MAX_EXPANDED:
                    raise HistoryImportError("Gate L2 decompression cap exceeded")
                lines += chunk.count(b"\\n")
                if lines > MAX_SOURCE_LINES:
                    raise HistoryImportError("Gate L2 line count exceeds budget")
    except (EOFError, OSError) as exc:
        raise HistoryImportError("truncated/invalid original Gate L2 gzip") from exc
    if expanded == 0:
        raise HistoryImportError("empty original Gate L2 content")
    return {"compressed_bytes": len(data), "expanded_bytes": expanded,
            "newlines_in_original": lines, "gzip_integrity_verified": True,
            "l2_event_schema_verified": False, "linked_book_replay_verified": False}


def download(client: httpx.Client, *, asset: str, family: str,
             hour: str, now: datetime | None = None) -> tuple[bytes, dict]:
    url = source_url(asset=asset, family=family, hour=hour, now=now)
    chunks = []
    seen = 0
    try:
        with client.stream("GET", url, follow_redirects=False,
                           headers={"Accept": "application/octet-stream"}) as response:
            if response.status_code != 200 or response.is_redirect:
                raise HistoryImportError(
                    f"Gate original L2 HTTP {response.status_code}; hour unverified"
                )
            length = response.headers.get("content-length")
            if length and (not length.isdecimal() or int(length) > MAX_COMPRESSED):
                raise HistoryImportError("Gate original L2 announced too many bytes")
            for chunk in response.iter_bytes():
                seen += len(chunk)
                if seen > MAX_COMPRESSED:
                    raise HistoryImportError("Gate original L2 exceeded transfer budget")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise HistoryImportError("Gate historical L2 network request failed") from exc
    data = b"".join(chunks)
    metadata = inspect_gzip(data)
    metadata.update({"source_url": url,
                     "original_source_sha256": hashlib.sha256(data).hexdigest()})
    return data, metadata


async def store(drive, *, raw: bytes, asset: str, family: str, hour: str,
                evidence: dict, root: Path, now: datetime | None = None) -> dict:
    expected_url = source_url(asset=asset, family=family, hour=hour, now=now)
    digest = hashlib.sha256(raw).hexdigest()
    if evidence.get("original_source_sha256") != digest or evidence.get(
        "source_url"
    ) != expected_url:
        raise HistoryImportError("Gate L2 archive identity/digest mismatch")
    raw_name = f"gate-hourly-l2-{asset}-{family}-{hour}--{digest[:16]}.gz"
    root.mkdir(parents=True, exist_ok=True)
    path = root / raw_name
    manifest_path = root / (raw_name + ".manifest.json")
    proof = {
        "schema_version": "rival-gate-hourly-l2-original-v1",
        "venue": "gate", "asset": asset, "contract": f"{asset}_USDT",
        "quote": "USDT", "family": family, "utc_hour": hour,
        "publication_start_is_attempt_floor": True,
        "source_url": expected_url, "original_file": raw_name,
        "original_sha256": digest, **{
            key: value for key, value in evidence.items()
            if key != "original_source_sha256"
        },
        "original_available_at": datetime.now(UTC).isoformat(),
        "original_hourly_source_complete": False,
        "historical_orderbook_continuity_verified": False,
        "live_quote_available_at_source_event": False,
        "model_training_eligible": False,
        "forward_model_promoted": False,
        "source_provider_signed_checksum": False,
    }
    # The first-observed timestamp intentionally changes on retrieval. Reusing
    # prior immutable evidence is managed by distinct filename digest, not
    # retroactive reassignment to the original event time.
    encoded = (json.dumps(proof, sort_keys=True, indent=2) + "\\n").encode()
    for target, bytes_ in ((path, raw), (manifest_path, encoded)):
        if target.exists() and target.read_bytes() != bytes_:
            raise HistoryImportError("Gate immutable L2 original conflict")
        if not target.exists():
            target.write_bytes(bytes_)
    first, second = _artifact(path, 1), _artifact(manifest_path, 1)
    batch = "gate-l2-" + digest[:24]
    obj = await drive.ensure_history_file(
        artifact=first, batch_id=batch, mime_type="application/gzip",
    )
    await drive.verify_downloaded_artifact(
        file_id=obj.file_id, parent_id=drive.folders.history, artifact=first,
    )
    meta = await drive.ensure_manifest_file(artifact=second, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=meta.file_id, parent_id=drive.folders.manifests, artifact=second,
    )
    return {"asset": asset, "family": family, "utc_hour": hour,
            "original_sha256": digest, "drive_sha256_readback_verified": True,
            "l2_hourly_complete": False, "model_training_eligible": False}


async def collect(*, hour: str, assets: tuple[str, ...],
                  family: str, root: Path) -> dict:
    source_url(asset=assets[0] if assets else "", family=family, hour=hour)
    if (not assets or len(assets) != len(set(assets))
        or any(asset not in ASSETS for asset in assets)):
        raise HistoryImportError("unbounded Gate depth research scope")
    result = {"schema": "rival-gate-hourly-original-attempt-v1",
              "source_hour": hour, "family": family, "readbacks": [],
              "unavailable": [], "all_historical_liquidity_complete": False}
    drive = _drive(Settings())
    try:
        with httpx.Client(timeout=45, follow_redirects=False) as client:
            for asset in assets:
                try:
                    raw, evidence = await asyncio.to_thread(
                        download, client, asset=asset, family=family, hour=hour,
                    )
                    result["readbacks"].append(await store(
                        drive, raw=raw, asset=asset, family=family, hour=hour,
                        evidence=evidence, root=root,
                    ))
                except (HistoryImportError, httpx.HTTPError, OSError) as exc:
                    result["unavailable"].append({
                        "asset": asset, "source": family, "reason": str(exc)[:180],
                    })
                await asyncio.sleep(1)
    finally:
        await drive.aclose()
    out = Path("data/native-reports/gate-hourly-l2-attempt.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded authentic Gate hourly depth samples")
    parser.add_argument("--hour", required=True, help="Completed UTC hour YYYYMMDDHH")
    parser.add_argument("--family", choices=FAMILIES, default="orderbooks_slice")
    parser.add_argument("--assets", nargs="+", choices=ASSETS, default=["BTC"])
    parser.add_argument("--root", type=Path, default=Path("data/gate-hourly-l2"))
    args = parser.parse_args()
    report = asyncio.run(collect(
        hour=args.hour, assets=tuple(args.assets), family=args.family,
        root=args.root,
    ))
    print(json.dumps({"verified_original_archives": len(report["readbacks"]),
                      "blocked": report["unavailable"],
                      "complete_history": False}, sort_keys=True))
    return 0 if report["readbacks"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
