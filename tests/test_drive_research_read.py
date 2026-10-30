"""Read-only private Drive model transfer verifies byte SHA and folder identity."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.archive.drive import (
    DriveArchiveError,
    DriveArchiveFolders,
    GoogleDriveArchiveClient,
)


@pytest.mark.asyncio
async def test_model_download_is_bounded_sha_verified_and_read_only():
    payload = b'{"research_only":true,"live_model_promoted":false}'
    checksum = hashlib.sha256(payload).hexdigest()
    accessed = []

    def handler(request: httpx.Request) -> httpx.Response:
        accessed.append((request.method, str(request.url)))
        assert request.method == "GET"
        if request.url.path == "/drive/v3/files":
            assert "'models' in parents" in request.url.params["q"]
            return httpx.Response(200, json={"files": [{
                "id": "idOne", "name": "candidate.json", "size": str(len(payload)),
                "parents": ["models"], "appProperties": {
                    "rival_kind": "model", "rival_sha256": checksum,
                },
            }]})
        assert request.url.path == "/drive/v3/files/idOne"
        assert request.url.params["alt"] == "media"
        return httpx.Response(200, content=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    drive = GoogleDriveArchiveClient(
        client_id="id", client_secret="secret", refresh_token="refresh",
        folders=DriveArchiveFolders(
            root="root", history="history", manifests="manifests",
            staging="staging", models="models",
        ), client=client,
    )
    drive._verify_folder_boundary = AsyncMock()
    drive._access_token = AsyncMock(return_value="token")
    try:
        assert await drive.read_verified_research_candidates() == [
            ("idOne", payload),
        ]
        assert len(accessed) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_corrupt_drive_model_bytes_rejected_before_registry():
    content = b"malformed"
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"files": [{
                "id": "modelOne", "name": "candidate.json",
                "size": str(len(content)), "parents": ["models"],
                "appProperties": {"rival_kind": "model",
                                  "rival_sha256": "a"*64},
            }]})
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    drive = GoogleDriveArchiveClient(
        client_id="id", client_secret="secret", refresh_token="token",
        folders=DriveArchiveFolders(
            root="root", history="history", manifests="manifests",
            staging="staging", models="models",
        ), client=client,
    )
    drive._verify_folder_boundary = AsyncMock()
    drive._access_token = AsyncMock(return_value="token")
    try:
        with pytest.raises(DriveArchiveError, match="byte identity"):
            await drive.read_verified_research_candidates()
    finally:
        await client.aclose()


def test_verified_history_schedule_requires_explicit_owner_flag():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/verified-history.yml"
    ).read_text(encoding="utf-8")
    assert "vars.RIVAL_HISTORY_MONTHLY_ENABLED == 'true'" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "live model promotion are separate" not in workflow
