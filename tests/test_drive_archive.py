from __future__ import annotations

import hashlib

import httpx
import pytest

from parlay_bot.archive.drive import DriveArchiveFolders, GoogleDriveArchiveClient
from parlay_bot.archive.models import ArchiveArtifact


@pytest.mark.asyncio
async def test_history_upload_is_bounded_staged_verified_and_idempotent(tmp_path) -> None:
    path = tmp_path / "batch.parquet"
    payload = b"parquet-test-payload"
    path.write_bytes(payload)
    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(payload).hexdigest()
    artifact = ArchiveArtifact(
        path=path,
        file_name="batch.parquet",
        size_bytes=len(payload),
        sha256=sha256,
        md5=md5,
        row_count=10,
    )

    folders = DriveArchiveFolders(
        root="root-id",
        history="history-id",
        manifests="manifests-id",
        staging="staging-id",
    )
    token_calls = 0
    upload_calls = 0
    moved = False
    bounded_search_parents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, upload_calls, moved

        if request.url.host == "oauth2.googleapis.com":
            token_calls += 1
            assert request.url.path == "/token"
            return httpx.Response(
                200,
                json={"access_token": "access-1", "expires_in": 3600},
                request=request,
            )

        if request.method == "GET" and request.url.path == "/drive/v3/files":
            query = request.url.params["q"]
            if "'history-id' in parents" in query:
                bounded_search_parents.append("history-id")
                files = (
                    [
                        {
                            "id": "file-1",
                            "name": artifact.file_name,
                            "size": str(artifact.size_bytes),
                            "md5Checksum": artifact.md5,
                            "parents": ["history-id"],
                        }
                    ]
                    if moved
                    else []
                )
                return httpx.Response(200, json={"files": files}, request=request)
            if "'staging-id' in parents" in query:
                bounded_search_parents.append("staging-id")
                return httpx.Response(200, json={"files": []}, request=request)
            raise AssertionError(f"unbounded Drive search: {query}")

        if request.method == "POST" and request.url.path == "/upload/drive/v3/files":
            upload_calls += 1
            assert request.url.params["uploadType"] == "resumable"
            assert request.headers["x-upload-content-length"] == str(len(payload))
            return httpx.Response(
                200,
                headers={"Location": "https://upload.example/session"},
                request=request,
            )

        if request.method == "PUT" and request.url.host == "upload.example":
            assert request.content == payload
            return httpx.Response(
                200,
                json={
                    "id": "file-1",
                    "name": artifact.file_name,
                    "size": str(artifact.size_bytes),
                    "md5Checksum": artifact.md5,
                    "parents": ["staging-id"],
                },
                request=request,
            )

        if request.method == "PATCH" and request.url.path == "/drive/v3/files/file-1":
            assert request.url.params["addParents"] == "history-id"
            assert request.url.params["removeParents"] == "staging-id"
            moved = True
            return httpx.Response(
                200,
                json={
                    "id": "file-1",
                    "name": artifact.file_name,
                    "size": str(artifact.size_bytes),
                    "md5Checksum": artifact.md5,
                    "parents": ["history-id"],
                },
                request=request,
            )

        raise AssertionError(f"unexpected Drive request: {request.method} {request.url}")

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
        first = await client.ensure_history_file(
            artifact=artifact,
            batch_id="batch-1",
        )
        second = await client.ensure_history_file(
            artifact=artifact,
            batch_id="batch-1",
        )

    assert first.file_id == "file-1"
    assert second.file_id == "file-1"
    assert first.parents == ("history-id",)
    assert upload_calls == 1
    assert token_calls == 1
    assert "root-id" not in bounded_search_parents
    assert set(bounded_search_parents) <= {"history-id", "staging-id"}


def test_drive_folder_boundary_rejects_duplicate_folder_ids() -> None:
    with pytest.raises(ValueError, match="distinct"):
        DriveArchiveFolders(
            root="same",
            history="same",
            manifests="manifests",
            staging="staging",
        )
