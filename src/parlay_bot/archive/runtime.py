from __future__ import annotations

from dataclasses import dataclass

from parlay_bot.archive.drive import DriveArchiveFolders, GoogleDriveArchiveClient
from parlay_bot.archive.queue import SQLiteArchiveQueue
from parlay_bot.archive.service import ColdArchiveService
from parlay_bot.archive.writer import ParquetArchiveWriter
from parlay_bot.config import Settings


@dataclass(frozen=True)
class ColdArchiveRuntime:
    service: ColdArchiveService
    drive: GoogleDriveArchiveClient


def build_cold_archive_runtime(settings: Settings) -> ColdArchiveRuntime | None:
    """Build the archive runtime only when explicitly enabled."""

    if not settings.rival_archive_enabled:
        return None

    required = {
        "RIVAL_DRIVE_ROOT_FOLDER_ID": settings.rival_drive_root_folder_id,
        "RIVAL_DRIVE_HISTORY_FOLDER_ID": settings.rival_drive_history_folder_id,
        "RIVAL_DRIVE_MANIFESTS_FOLDER_ID": settings.rival_drive_manifests_folder_id,
        "RIVAL_DRIVE_STAGING_FOLDER_ID": settings.rival_drive_staging_folder_id,
        "RIVAL_DRIVE_OAUTH_CLIENT_ID": settings.rival_drive_oauth_client_id,
        "RIVAL_DRIVE_OAUTH_CLIENT_SECRET": (
            settings.rival_drive_oauth_client_secret.get_secret_value()
            if settings.rival_drive_oauth_client_secret is not None
            else None
        ),
        "RIVAL_DRIVE_OAUTH_REFRESH_TOKEN": (
            settings.rival_drive_oauth_refresh_token.get_secret_value()
            if settings.rival_drive_oauth_refresh_token is not None
            else None
        ),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "RivaL cold archive is enabled but required secret/config values are missing: "
            + ", ".join(sorted(missing))
        )

    folders = DriveArchiveFolders(
        root=str(settings.rival_drive_root_folder_id),
        history=str(settings.rival_drive_history_folder_id),
        manifests=str(settings.rival_drive_manifests_folder_id),
        staging=str(settings.rival_drive_staging_folder_id),
    )
    drive = GoogleDriveArchiveClient(
        client_id=str(settings.rival_drive_oauth_client_id),
        client_secret=settings.rival_drive_oauth_client_secret.get_secret_value(),
        refresh_token=settings.rival_drive_oauth_refresh_token.get_secret_value(),
        folders=folders,
        chunk_size_bytes=settings.rival_archive_chunk_mib * 1024 * 1024,
    )
    queue = SQLiteArchiveQueue(settings.rival_db_path)
    writer = ParquetArchiveWriter(settings.rival_archive_local_dir)
    return ColdArchiveRuntime(
        service=ColdArchiveService(queue=queue, writer=writer, drive=drive),
        drive=drive,
    )
