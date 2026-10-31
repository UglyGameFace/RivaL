"""Explicit, verified promotion of offline research into RivaL's private Drive.

Uses the existing bounded Drive staging -> history + manifest flow. The
command is separate from the live bot and cannot modify prediction tables.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from parlay_bot.archive.drive import DriveArchiveFolders, GoogleDriveArchiveClient
from parlay_bot.archive.models import ArchiveArtifact
from parlay_bot.config import Settings
from parlay_bot.research.free_history import SCHEMA, HistoryImportError

SOURCES = {
    "binance_um_klines", "football_data",
    "binance_um_funding", "binance_um_mark_klines",
    "binance_um_index_klines", "binance_um_premium_klines",
    "polymarket_token_history", "kalshi_contract_candles",
    "binance_um_daily_metrics",
}


@dataclass(frozen=True)
class ResearchPublication:
    batch_id: str
    history: ArchiveArtifact
    manifest: ArchiveArtifact
    source: str


def _artifact(path: Path, rows: int) -> ArchiveArtifact:
    data = path.read_bytes()
    return ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        row_count=rows,
    )


def prepare(manifest_path: Path) -> ResearchPublication:
    manifest_path = manifest_path.resolve(strict=True)
    try:
        info = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            info.get("schema_version") != SCHEMA
            or info.get("source") not in SOURCES
            or info.get("type") != "offline_historical_research_only"
            or info.get("retrospective") is not True
            or info.get("prospective_forecast") is not False
            or info.get("live_trading_authorized") is not False
        ):
            raise HistoryImportError("unverified or nonresearch manifest")
        rows = info.get("records")
        name = info.get("file")
        if (
            type(rows) is not int or not 0 < rows <= 100_000
            or not isinstance(name, str) or Path(name).name != name
            or not name.endswith(".ndjson")
            or not manifest_path.name.endswith(".manifest.json")
            or not name[:-7] == manifest_path.name[:-14]
        ):
            raise HistoryImportError("unexpected research file or count")
        path = (manifest_path.parent / name).resolve(strict=True)
        if path.parent != manifest_path.parent:
            raise HistoryImportError("data path escapes research partition")
        history = _artifact(path, rows)
        if (
            history.sha256 != info.get("data_sha256")
            or len(path.read_bytes().splitlines()) != rows
        ):
            raise HistoryImportError("research content hash or row count changed")
        manifest = _artifact(manifest_path, 1)
        identity = f"{info['source']}:{info.get('source_url')}:{history.sha256}"
        batch_id = "external-research-v1-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:24]
        return ResearchPublication(batch_id, history, manifest, info["source"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise HistoryImportError("research manifest could not be verified") from exc


async def publish(drive: GoogleDriveArchiveClient, item: ResearchPublication) -> dict:
    history = await drive.ensure_history_file(
        artifact=item.history, batch_id=item.batch_id,
        mime_type="application/x-ndjson",
    )
    manifest = await drive.ensure_manifest_file(
        artifact=item.manifest, batch_id=item.batch_id,
    )
    # Read back both *actual* stored objects, verify SHA-256, and fail closed.
    # A completed resumable upload alone is not proof of recoverability.
    await drive.verify_downloaded_artifact(
        file_id=history.file_id, parent_id=drive.folders.history,
        artifact=item.history,
    )
    await drive.verify_downloaded_artifact(
        file_id=manifest.file_id, parent_id=drive.folders.manifests,
        artifact=item.manifest,
    )
    return {
        "source": item.source, "batch_id": item.batch_id,
        "history_id": history.file_id, "manifest_id": manifest.file_id,
        "rows": item.history.row_count, "warehouse_promoted": True, "restore_sha256_verified": True,
        "live_predictions_modified": False,
    }


def _drive(settings: Settings) -> GoogleDriveArchiveClient:
    required = {
        "RIVAL_DRIVE_ROOT_FOLDER_ID": settings.rival_drive_root_folder_id,
        "RIVAL_DRIVE_HISTORY_FOLDER_ID": settings.rival_drive_history_folder_id,
        "RIVAL_DRIVE_MANIFESTS_FOLDER_ID": settings.rival_drive_manifests_folder_id,
        "RIVAL_DRIVE_STAGING_FOLDER_ID": settings.rival_drive_staging_folder_id,
        "RIVAL_DRIVE_OAUTH_CLIENT_ID": settings.rival_drive_oauth_client_id,
        "RIVAL_DRIVE_OAUTH_CLIENT_SECRET": settings.rival_drive_oauth_client_secret,
        "RIVAL_DRIVE_OAUTH_REFRESH_TOKEN": settings.rival_drive_oauth_refresh_token,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise HistoryImportError("missing private Drive archive configuration: " +
                                 ", ".join(sorted(missing)))
    return GoogleDriveArchiveClient(
        client_id=settings.rival_drive_oauth_client_id,
        client_secret=settings.rival_drive_oauth_client_secret.get_secret_value(),
        refresh_token=settings.rival_drive_oauth_refresh_token.get_secret_value(),
        folders=DriveArchiveFolders(
            root=settings.rival_drive_root_folder_id,
            history=settings.rival_drive_history_folder_id,
            manifests=settings.rival_drive_manifests_folder_id,
            staging=settings.rival_drive_staging_folder_id,
            models=settings.rival_drive_models_folder_id,
            reports=settings.rival_drive_reports_folder_id,
        ),
        chunk_size_bytes=settings.rival_archive_chunk_mib * 1024 * 1024,
    )


async def _run(item: ResearchPublication) -> dict:
    drive = _drive(Settings())
    try:
        return await publish(drive, item)
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit private RivaL research archive")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--publish", action="store_true", help="Perform verified Drive upload")
    args = parser.parse_args(argv)
    try:
        item = prepare(args.manifest)
        if args.publish:
            result = asyncio.run(_run(item))
        else:
            result = {"source": item.source, "batch_id": item.batch_id,
                      "rows": item.history.row_count,
                      "data_sha256": item.history.sha256,
                      "dry_run": True}
    except (HistoryImportError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Research publication failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
