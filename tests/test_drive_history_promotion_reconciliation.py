"""Source-exact Drive history promotion after ambiguous HTTP results.

The test models the *same immutable file ID* transitioning between staging
and history. A timeout cannot authorize a second upload of original bytes.
"""
from __future__ import annotations

import hashlib

import httpx
import pytest

from parlay_bot.archive.drive import (
    MAX_TRANSIENT_UPLOAD_RETRIES,
    DriveArchiveError,
    DriveArchiveFolders,
    GoogleDriveArchiveClient,
)
from parlay_bot.archive.models import ArchiveArtifact


@pytest.mark.asyncio
@pytest.mark.parametrize(("scenario", "success", "patches", "uploads"), [
    ("timeout_committed", True, 1, 1),
    ("not_found_committed", True, 1, 1),
    ("transient_503", True, 2, 1),
    ("staged_resume", True, 1, 0),
    ("timeout_vanished", False, 1, 1),
    ("lookup_timeout", False, 1, 1),
    ("permission_denied", False, 1, 1),
    ("changed_remote_checksum", False, 1, 1),
    ("different_remote_id", False, 1, 1),
    ("transient_exhausted", False, MAX_TRANSIENT_UPLOAD_RETRIES + 1, 1),
])
async def test_reconcile_original_promotion_without_reupload(
    tmp_path, monkeypatch, scenario, success, patches, uploads,
):
    from parlay_bot.archive import drive as drive_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(drive_module.asyncio, "sleep", no_sleep)
    payload = b"source-authentic immutable candle history bytes"
    file = tmp_path / "original-source.ndjson"
    file.write_bytes(payload)
    artifact = ArchiveArtifact(
        path=file, file_name=file.name, size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        row_count=1,
    )
    state = {
        "location": "staging" if scenario == "staged_resume" else "none",
        "post": 0, "put": 0, "patch": 0, "checks": 0,
    }

    def item(location):
        return {
            "id": "source-1",
            "name": file.name,
            "size": str(len(payload)),
            "md5Checksum": (
                "0" * 32 if scenario == "changed_remote_checksum"
                and location == "history" else artifact.md5
            ),
            "parents": [location + "-id"],
        }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={
                "access_token": "private", "expires_in": 3600,
            })
        if req.method == "GET" and req.url.path.startswith("/drive/v3/files/"):
            folder = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "id": folder, "name": folder,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [] if folder == "root-id" else ["root-id"],
                "trashed": False,
            })
        if req.method == "GET" and req.url.path == "/drive/v3/files":
            state["checks"] += 1
            query = req.url.params["q"]
            assert "rival_batch_id" in query and "original-batch" in query
            if "'history-id' in parents" in query:
                location = "history"
            elif "'staging-id' in parents" in query:
                location = "staging"
            else:
                raise AssertionError("unbounded source search")
            if scenario == "lookup_timeout" and state["patch"] > 0:
                raise httpx.ReadTimeout("private Drive URL and response", request=req)
            files = ([item(location)] if state["location"] == location else [])
            return httpx.Response(200, json={"files": files})
        if req.method == "POST" and req.url.path == "/upload/drive/v3/files":
            state["post"] += 1
            return httpx.Response(200, headers={
                "Location": "https://upload.example/source-session",
            })
        if req.method == "PUT" and req.url.host == "upload.example":
            state["put"] += 1
            assert req.content == payload
            assert state["location"] == "none"
            state["location"] = "staging"
            return httpx.Response(200, json=item("staging"))
        if req.method == "PATCH" and req.url.path == "/drive/v3/files/source-1":
            state["patch"] += 1
            assert req.url.params["addParents"] == "history-id"
            assert req.url.params["removeParents"] == "staging-id"
            if scenario == "permission_denied":
                return httpx.Response(403, json={"error": "private-denial"})
            if scenario == "lookup_timeout":
                raise httpx.ReadTimeout("private promotion response", request=req)
            if scenario == "timeout_vanished":
                state["location"] = "none"
                raise httpx.ReadTimeout("private request detail", request=req)
            if scenario in {"timeout_committed", "not_found_committed",
                            "changed_remote_checksum", "different_remote_id"}:
                state["location"] = "history"
                if scenario == "not_found_committed":
                    return httpx.Response(404)
                if scenario == "different_remote_id":
                    bad = item("history")
                    bad["id"] = "unexpected-source-2"
                    return httpx.Response(200, json=bad)
                raise httpx.ReadTimeout("private request detail", request=req)
            if scenario == "transient_exhausted":
                return httpx.Response(503)
            if scenario == "transient_503" and state["patch"] == 1:
                return httpx.Response(503)
            state["location"] = "history"
            return httpx.Response(200, json=item("history"))
        raise AssertionError(f"unexpected request {req.method} {req.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        drive = GoogleDriveArchiveClient(
            client_id="client", client_secret="secret", refresh_token="refresh",
            folders=DriveArchiveFolders(
                root="root-id", history="history-id",
                manifests="manifests-id", staging="staging-id",
            ),
            client=http, chunk_size_bytes=256 * 1024,
        )
        if success:
            promoted = await drive.ensure_history_file(
                artifact=artifact, batch_id="original-batch",
                mime_type="application/x-ndjson",
            )
            assert promoted.file_id == "source-1"
            assert promoted.parents == ("history-id",)
            again = await drive.ensure_history_file(
                artifact=artifact, batch_id="original-batch",
                mime_type="application/x-ndjson",
            )
            assert again.file_id == "source-1"
        else:
            with pytest.raises(DriveArchiveError) as exc:
                await drive.ensure_history_file(
                    artifact=artifact, batch_id="original-batch",
                    mime_type="application/x-ndjson",
                )
            assert "private" not in str(exc.value)
            assert "source-session" not in str(exc.value)

    assert state["patch"] == patches
    assert state["put"] == uploads
    assert state["post"] == uploads
    assert state["checks"] <= 2 * (MAX_TRANSIENT_UPLOAD_RETRIES + 3) + 4
