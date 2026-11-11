from __future__ import annotations

import hashlib

import httpx
import pytest

from parlay_bot.archive.drive import DriveArchiveFolders, GoogleDriveArchiveClient
from parlay_bot.archive.models import ArchiveArtifact


@pytest.mark.parametrize("mime_type", ("application/vnd.apache.parquet", "application/zip", "application/gzip", "text/plain"))
@pytest.mark.asyncio
async def test_history_upload_is_bounded_staged_verified_and_idempotent(tmp_path, mime_type: str) -> None:
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

        if request.method == "GET" and request.url.path.startswith("/drive/v3/files/"):
            folder_id = request.url.path.rsplit("/", 1)[1]
            parents = [] if folder_id == "root-id" else ["root-id"]
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
            mime_type=mime_type,
        )
        second = await client.ensure_history_file(
            artifact=artifact,
            batch_id="batch-1",
            mime_type=mime_type,
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


@pytest.mark.asyncio
async def test_resumable_upload_recovers_from_transient_503(tmp_path, monkeypatch) -> None:
    from parlay_bot.archive import drive as drive_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(drive_module.asyncio, "sleep", no_sleep)
    payload = b"zip-bytes-for-retry"
    path = tmp_path / "BTCUSDT-1h-2024-08.zip"
    path.write_bytes(payload)
    artifact = ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(), row_count=1,
    )
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        if request.method == "PUT":
            puts.append(request.headers["content-range"])
            if len(puts) == 1:
                return httpx.Response(503)
            if request.headers["content-range"] == f"bytes */{len(payload)}":
                return httpx.Response(308)  # nothing persisted yet
            return httpx.Response(200, json={
                "id": "f1", "name": path.name, "size": str(len(payload)),
                "md5Checksum": artifact.md5, "parents": ["staging-id"],
            })
        raise AssertionError(f"unexpected {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GoogleDriveArchiveClient(
            client_id="c", client_secret="s", refresh_token="r",
            folders=DriveArchiveFolders(root="root-id", history="history-id",
                                        manifests="manifests-id", staging="staging-id"),
            client=http, chunk_size_bytes=256 * 1024,
        )
        stored = await client._upload_resumable(
            artifact=artifact, parent_id="staging-id", batch_id="b",
            kind="history", mime_type="application/zip",
        )
    assert stored.file_id == "f1"
    full = f"bytes 0-{len(payload) - 1}/{len(payload)}"
    assert puts == [full, f"bytes */{len(payload)}", full]


@pytest.mark.parametrize(("body", "expected"), (
    ({"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
     "Google OAuth refresh failed with 400 (invalid_grant)"),
    (None, "Google OAuth refresh failed with 400"),
))
@pytest.mark.asyncio
async def test_a_rejected_sign_in_names_googles_reason_never_the_token(body, expected) -> None:
    from parlay_bot.archive.drive import DriveArchiveError

    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(400, text="not json")
        return httpx.Response(400, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GoogleDriveArchiveClient(
            client_id="c", client_secret="s", refresh_token="secret-refresh",
            folders=DriveArchiveFolders(root="root-id", history="history-id",
                                        manifests="manifests-id", staging="staging-id"),
            client=http, chunk_size_bytes=256 * 1024,
        )
        with pytest.raises(DriveArchiveError) as raised:
            await client._access_token()
    assert str(raised.value) == expected
    assert "secret-refresh" not in str(raised.value)
