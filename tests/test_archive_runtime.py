from __future__ import annotations

import pytest

from parlay_bot.archive.runtime import build_cold_archive_runtime
from parlay_bot.config import Settings
from parlay_bot.storage.hot import SQLiteHotStore


def test_archive_runtime_is_off_by_default(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        rival_db_path=str(tmp_path / "rival.sqlite"),
    )

    assert build_cold_archive_runtime(settings) is None


def test_enabled_archive_requires_complete_private_configuration(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        rival_db_path=str(tmp_path / "rival.sqlite"),
        rival_archive_enabled=True,
    )

    with pytest.raises(RuntimeError, match="RIVAL_DRIVE_ROOT_FOLDER_ID"):
        build_cold_archive_runtime(settings)


@pytest.mark.asyncio
async def test_enabled_archive_builds_without_making_network_request(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        rival_db_path=str(tmp_path / "rival.sqlite"),
        rival_archive_enabled=True,
        rival_archive_local_dir=str(tmp_path / "archive"),
        rival_drive_root_folder_id="root",
        rival_drive_history_folder_id="history",
        rival_drive_manifests_folder_id="manifests",
        rival_drive_staging_folder_id="staging",
        rival_drive_oauth_client_id="client-id",
        rival_drive_oauth_client_secret="client-secret",
        rival_drive_oauth_refresh_token="refresh-token",
    )

    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    runtime = build_cold_archive_runtime(settings, store=store)
    assert runtime is not None
    try:
        assert runtime.drive.folders.root == "root"
        assert runtime.drive.folders.history == "history"
        assert runtime.service.queue.store is store
    finally:
        await runtime.drive.aclose()
