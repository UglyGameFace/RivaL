from __future__ import annotations

import hashlib

import httpx
import pytest

from parlay_bot.archive.drive import (
    DriveArchiveError,
    DriveArchiveFolders,
    GoogleDriveArchiveClient,
)
from parlay_bot.archive.models import ArchiveArtifact


@pytest.mark.asyncio
async def test_archive_rejects_child_folder_outside_rival_root(tmp_path) -> None:
    path = tmp_path / "batch.parquet"
    payload = b"archive-boundary-test"
    path.write_bytes(payload)
    artifact = ArchiveArtifact(
        path=path,
        file_name="batch.parquet",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        row_count=1,
    )
    folders = DriveArchiveFolders(
        root="root-id",
        history="history-id",
        manifests="manifests-id",
        staging="staging-id",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={"access_token": "access-1", "expires_in": 3600},
                request=request,
            )

        if request.method == "GET" and request.url.path.startswith("/drive/v3/files/"):
            folder_id = request.url.path.rsplit("/", 1)[1]
            parents = [] if folder_id == "root-id" else ["root-id"]
            if folder_id == "history-id":
                parents = ["other-root-id"]
            return httpx.Response(
                200,
                json={
                    "id": folder_id,
                    "name": folder_id,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": parents,
                    "trashed": False,
                },
                request=request,
            )

        raise AssertionError("archive must stop before search or upload")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GoogleDriveArchiveClient(
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
            folders=folders,
            client=http_client,
            chunk_size_bytes=256 * 1024,
        )
        with pytest.raises(
            DriveArchiveError,
            match="outside the RivaL Data Warehouse root",
        ):
            await client.ensure_history_file(
                artifact=artifact,
                batch_id="batch-outside",
            )
