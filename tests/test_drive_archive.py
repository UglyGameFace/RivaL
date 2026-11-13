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


@pytest.mark.parametrize("recovery", ("committed", "absent", "partial", "status_timeout"))
@pytest.mark.asyncio
async def test_timed_out_drive_chunk_uses_authoritative_session_offset(
    tmp_path, monkeypatch, recovery,
) -> None:
    """Never resend source bytes until the resumable session proves the offset."""
    from parlay_bot.archive import drive as drive_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(drive_module.asyncio, "sleep", no_sleep)
    payload = b"verified-original-historical-bytes"
    path = tmp_path / "native.ndjson"
    path.write_bytes(payload)
    artifact = ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(), row_count=1,
    )
    seen = []
    completed = {
        "id": "f1", "name": path.name, "size": str(len(payload)),
        "md5Checksum": artifact.md5, "parents": ["staging-id"],
    }
    full = f"bytes 0-{len(payload) - 1}/{len(payload)}"
    partial = f"bytes 4-{len(payload) - 1}/{len(payload)}"
    query = f"bytes */{len(payload)}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.host == "www.googleapis.com":
            return httpx.Response(
                200, headers={"Location": "https://upload.example/s"}
            )
        assert request.method == "PUT" and request.url.host == "upload.example"
        seen.append((request.headers["content-range"], request.content))
        if len(seen) == 1:
            assert seen[-1] == (full, payload)
            raise httpx.ReadTimeout("timed-out-committed-state-unknown", request=request)
        if request.headers["content-range"] == query:
            assert request.content == b""
            if recovery == "committed":
                return httpx.Response(200, json=completed)
            if recovery == "status_timeout" and len(seen) == 2:
                raise httpx.ReadTimeout("probe lost response", request=request)
            if recovery == "partial":
                return httpx.Response(308, headers={"Range": "bytes=0-3"})
            return httpx.Response(308)
        if recovery == "partial":
            assert seen[-1] == (partial, payload[4:])
        else:
            assert seen[-1] == (full, payload)
        return httpx.Response(200, json=completed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        drive = GoogleDriveArchiveClient(
            client_id="client", client_secret="secret", refresh_token="refresh",
            folders=DriveArchiveFolders(
                root="root-id", history="history-id",
                manifests="manifests-id", staging="staging-id",
            ), client=http, chunk_size_bytes=256 * 1024,
        )
        stored = await drive._upload_resumable(
            artifact=artifact, parent_id="staging-id",
            batch_id="native-source-verified", kind="history",
            mime_type="application/x-ndjson",
        )

    assert stored.file_id == "f1"
    assert seen[0] == (full, payload)
    assert seen[1] == (query, b"")
    if recovery == "committed":
        assert len(seen) == 2, "already committed source bytes never resend"
    elif recovery == "status_timeout":
        assert [x[0] for x in seen] == [full, query, query, full]
    else:
        assert len(seen) == 3


@pytest.mark.asyncio
async def test_timed_out_drive_status_probes_fail_closed_before_reupload(
    tmp_path, monkeypatch,
) -> None:
    from parlay_bot.archive import drive as drive_module
    from parlay_bot.archive.drive import MAX_TRANSIENT_UPLOAD_RETRIES, DriveArchiveError

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(drive_module.asyncio, "sleep", no_sleep)
    payload = b"immutable-original-bytes"
    path = tmp_path / "BTC.ndjson"
    path.write_bytes(payload)
    artifact = ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(), row_count=1,
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        assert request.method == "PUT"
        seen.append((request.headers["content-range"], request.content))
        raise httpx.ReadTimeout("socket timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        drive = GoogleDriveArchiveClient(
            client_id="client", client_secret="secret", refresh_token="PRIVATE",
            folders=DriveArchiveFolders(
                root="root-id", history="history-id",
                manifests="manifests-id", staging="staging-id",
            ), client=http, chunk_size_bytes=256 * 1024,
        )
        with pytest.raises(DriveArchiveError, match="status probe recovery budget") as exc:
            await drive._upload_resumable(
                artifact=artifact, parent_id="staging-id",
                batch_id="native-source-verified", kind="history",
                mime_type="application/x-ndjson",
            )
    assert "PRIVATE" not in str(exc.value)
    assert len(seen) == MAX_TRANSIENT_UPLOAD_RETRIES + 1
    assert seen[0][1] == payload
    assert all(data == b"" for _, data in seen[1:])


@pytest.mark.asyncio
async def test_drive_session_rejects_out_of_bounds_remote_offset(
    tmp_path, monkeypatch,
) -> None:
    from parlay_bot.archive import drive as drive_module
    from parlay_bot.archive.drive import DriveArchiveError

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(drive_module.asyncio, "sleep", no_sleep)
    payload = b"native-data"
    path = tmp_path / "native.ndjson"
    path.write_bytes(payload)
    artifact = ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(), row_count=1,
    )
    puts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/s"})
        assert request.method == "PUT"
        puts.append(request.headers["content-range"])
        if len(puts) == 1:
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(
            308, headers={"Range": f"bytes=0-{len(payload) + 1000}"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        drive = GoogleDriveArchiveClient(
            client_id="client", client_secret="secret", refresh_token="refresh",
            folders=DriveArchiveFolders(
                root="root-id", history="history-id",
                manifests="manifests-id", staging="staging-id",
            ), client=http, chunk_size_bytes=256 * 1024,
        )
        with pytest.raises(DriveArchiveError, match="out-of-bounds Range"):
            await drive._upload_resumable(
                artifact=artifact, parent_id="staging-id",
                batch_id="native-source-verified", kind="history",
                mime_type="application/x-ndjson",
            )
    assert len(puts) == 2, "never reupload based on an impossible remote offset"



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
