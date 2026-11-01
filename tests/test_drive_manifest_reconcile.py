"""Divergent Drive manifests must heal instead of wedging the batch.

The manifest payload carries a build timestamp, so any interruption
between a manifest upload and mark_complete leaves a remote manifest
whose checksum can never match a rebuild. Before this fix, every retry
failed identically and cold archiving stuck on that batch forever. A
manifest is a regenerable index (rebuilt from the same queued batch and
history file), so a mismatch trashes the remote copy — recoverable, and
invisible to future searches — and uploads fresh. Immutable artifacts
(history, models, reports) keep failing loudly on mismatch.
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from parlay_bot.archive.drive import (
    DriveArchiveError,
    DriveArchiveFolders,
    GoogleDriveArchiveClient,
)
from parlay_bot.archive.models import ArchiveArtifact


def _artifact(tmp_path, payload: bytes):
    path = tmp_path / "manifest.json"
    path.write_bytes(payload)
    return ArchiveArtifact(
        path=path,
        file_name="manifest__batch=batch-9.json",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        row_count=3,
    )


def _folders():
    return DriveArchiveFolders(
        root="root-id",
        history="history-id",
        manifests="manifests-id",
        staging="staging-id",
    )


def _client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return GoogleDriveArchiveClient(
        client_id="client",
        client_secret="<redacted>",
        refresh_token="<redacted>",
        folders=_folders(),
        client=http_client,
        chunk_size_bytes=256 * 1024,
    ), http_client


def _folder_meta(request):
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


@pytest.mark.asyncio
async def test_manifest_mismatch_trashes_and_reuploads(tmp_path):
    payload = b'{"batch_id": "batch-9", "row_count": 3}\n'
    artifact = _artifact(tmp_path, payload)
    calls = {"trashed": 0, "uploads": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={"access_token": "access-1", "expires_in": 3600},
                request=request,
            )
        if request.method == "GET" and request.url.path.startswith("/drive/v3/files/"):
            return _folder_meta(request)
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            query = request.url.params["q"]
            assert "'manifests-id' in parents" in query
            return httpx.Response(200, json={"files": [{
                "id": "old-manifest",
                "name": artifact.file_name,
                "size": str(len(payload) + 9),
                "md5Checksum": "deadbeefdeadbeefdeadbeefdeadbeef",
                "parents": ["manifests-id"],
            }]}, request=request)
        if request.method == "PATCH" and request.url.path == "/drive/v3/files/old-manifest":
            assert json.loads(request.content) == {"trashed": True}
            calls["trashed"] += 1
            return httpx.Response(
                200, json={"id": "old-manifest", "trashed": True}, request=request,
            )
        if request.method == "POST" and request.url.path == "/upload/drive/v3/files":
            calls["uploads"] += 1
            return httpx.Response(
                200, headers={"Location": "https://upload.example/session"},
                request=request,
            )
        if request.method == "PUT" and request.url.host == "upload.example":
            assert request.content == payload
            return httpx.Response(200, json={
                "id": "new-manifest",
                "name": artifact.file_name,
                "size": str(artifact.size_bytes),
                "md5Checksum": artifact.md5,
                "parents": ["manifests-id"],
            }, request=request)
        raise AssertionError(f"unexpected Drive request: {request.method} {request.url}")

    client, http_client = _client(handler)
    try:
        result = await client.ensure_manifest_file(
            artifact=artifact, batch_id="batch-9",
        )
    finally:
        await http_client.aclose()
    assert result.file_id == "new-manifest"
    assert calls == {"trashed": 1, "uploads": 1}


@pytest.mark.asyncio
async def test_staged_history_mismatch_still_fails_without_trash(tmp_path):
    payload = b"parquet-payload"
    artifact = _artifact(tmp_path, payload)
    calls = {"trashed": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={"access_token": "access-1", "expires_in": 3600},
                request=request,
            )
        if request.method == "GET" and request.url.path.startswith("/drive/v3/files/"):
            return _folder_meta(request)
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            query = request.url.params["q"]
            if "'history-id' in parents" in query:
                return httpx.Response(200, json={"files": []}, request=request)
            if "'staging-id' in parents" in query:
                return httpx.Response(200, json={"files": [{
                    "id": "old-staged",
                    "name": artifact.file_name,
                    "size": str(len(payload) + 1),
                    "md5Checksum": "deadbeefdeadbeefdeadbeefdeadbeef",
                    "parents": ["staging-id"],
                }]}, request=request)
            raise AssertionError(f"unbounded Drive search: {query}")
        if request.method == "PATCH":
            calls["trashed"] += 1
            raise AssertionError("immutable artifacts must never be trashed")
        raise AssertionError(f"unexpected Drive request: {request.method} {request.url}")

    client, http_client = _client(handler)
    try:
        with pytest.raises(DriveArchiveError, match="mismatch"):
            await client.ensure_history_file(
                artifact=artifact, batch_id="batch-9",
            )
    finally:
        await http_client.aclose()
    assert calls["trashed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("divergent", [False, True])
async def test_identical_drive_model_duplicates_are_readback_verified(
    tmp_path, divergent,
):
    """Never delete or accept an ambiguous duplicate without SHA byte readback."""
    payload = b'{"research_only":true,"model":"candidate"}'
    artifact = _artifact(tmp_path, payload)
    media_reads = []
    writes = []
    folders = DriveArchiveFolders(
        root="root-id", history="history-id", manifests="manifests-id",
        staging="staging-id", models="models-id", reports="reports-id",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={
                "access_token": "access-1", "expires_in": 3600,
            }, request=request)
        if request.method in {"PATCH", "POST", "DELETE", "PUT"}:
            writes.append(str(request.url))
            raise AssertionError("duplicate reconciliation must be read-only")
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {
                    "id": "model-b", "name": artifact.file_name,
                    "size": str(artifact.size_bytes),
                    "md5Checksum": artifact.md5, "parents": ["models-id"],
                },
                {
                    "id": "model-a", "name": artifact.file_name,
                    "size": str(artifact.size_bytes),
                    "md5Checksum": artifact.md5, "parents": ["models-id"],
                },
            ]}, request=request)
        if request.url.path.startswith("/drive/v3/files/"):
            key = request.url.path.rsplit("/", 1)[-1]
            if key not in ("model-a", "model-b"):
                return _folder_meta(request)
            if request.url.params.get("alt") == "media":
                media_reads.append(key)
                result = payload if not (divergent and key == "model-b") else (
                    b"not-the-same-bytes"
                )
                return httpx.Response(200, content=result, request=request)
            return httpx.Response(200, json={
                "id": key, "name": artifact.file_name,
                "size": str(artifact.size_bytes),
                "md5Checksum": artifact.md5,
                "parents": ["models-id"], "trashed": False,
            }, request=request)
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GoogleDriveArchiveClient(
        client_id="client", client_secret="<redacted>",
        refresh_token="<redacted>", folders=folders, client=transport,
        chunk_size_bytes=256 * 1024,
    )
    try:
        if divergent:
            with pytest.raises(DriveArchiveError, match="restore|SHA256|exceeds"):
                await client.ensure_research_output(
                    artifact=artifact, batch_id="candidate-abc", kind="model",
                )
        else:
            obj = await client.ensure_research_output(
                artifact=artifact, batch_id="candidate-abc", kind="model",
            )
            assert obj.file_id == "model-a"
            assert set(media_reads) == {"model-a", "model-b"}
    finally:
        await transport.aclose()
    assert not writes
