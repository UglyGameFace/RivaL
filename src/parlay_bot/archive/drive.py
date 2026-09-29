from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from parlay_bot.archive.models import ArchiveArtifact, DriveObject


class DriveArchiveError(RuntimeError):
    """RivaL cold-archive Drive operation failed safely."""


@dataclass(frozen=True)
class DriveArchiveFolders:
    root: str
    history: str
    manifests: str
    staging: str

    def __post_init__(self) -> None:
        values = (self.root, self.history, self.manifests, self.staging)
        if any(not value.strip() for value in values):
            raise ValueError("all RivaL Drive archive folder IDs are required")
        if len(set(values)) != len(values):
            raise ValueError("RivaL Drive archive folder IDs must be distinct")


class GoogleDriveArchiveClient:
    """Narrow Drive client with no root listing and no arbitrary delete/update surface."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    DRIVE_URL = "https://www.googleapis.com/drive/v3"
    UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folders: DriveArchiveFolders,
        client: httpx.AsyncClient | None = None,
        chunk_size_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if not client_id or not client_secret or not refresh_token:
            raise ValueError("Google OAuth client ID, secret, and refresh token are required")
        if chunk_size_bytes <= 0 or chunk_size_bytes % (256 * 1024) != 0:
            raise ValueError("Drive chunk size must be a positive multiple of 256 KiB")

        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self.folders = folders
        self._chunk_size = chunk_size_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._boundary_lock = asyncio.Lock()
        self._boundary_verified = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._token_expires_at - 60:
            return self._token

        async with self._token_lock:
            if self._token is not None and time.monotonic() < self._token_expires_at - 60:
                return self._token

            response = await self._client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise DriveArchiveError(
                    f"Google OAuth refresh failed with {response.status_code}"
                )
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("access_token"):
                raise DriveArchiveError("Google OAuth refresh returned no access token")

            self._token = str(payload["access_token"])
            try:
                expires_in = max(int(payload.get("expires_in", 3600)), 120)
            except (TypeError, ValueError):
                expires_in = 3600
            self._token_expires_at = time.monotonic() + expires_in
            return self._token

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token()}"}

    @staticmethod
    def _escape_query(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _object(payload: dict[str, Any]) -> DriveObject:
        file_id = payload.get("id")
        name = payload.get("name")
        if not file_id or not name:
            raise DriveArchiveError("Google Drive response is missing file identity")

        size: int | None = None
        if payload.get("size") is not None:
            try:
                size = int(payload["size"])
            except (TypeError, ValueError) as exc:
                raise DriveArchiveError("Google Drive returned an invalid file size") from exc

        parents = payload.get("parents") or []
        if not isinstance(parents, list):
            raise DriveArchiveError("Google Drive returned invalid parent metadata")

        return DriveObject(
            file_id=str(file_id),
            name=str(name),
            size_bytes=size,
            md5=str(payload["md5Checksum"]) if payload.get("md5Checksum") else None,
            parents=tuple(str(item) for item in parents),
        )


    async def _verify_folder_boundary(self) -> None:
        """Prove configured archive folders are direct children of the configured RivaL root."""

        if self._boundary_verified:
            return

        async with self._boundary_lock:
            if self._boundary_verified:
                return

            headers = await self._headers()

            async def folder_metadata(folder_id: str) -> dict[str, Any]:
                response = await self._client.get(
                    f"{self.DRIVE_URL}/files/{folder_id}",
                    params={"fields": "id,name,mimeType,parents,trashed"},
                    headers=headers,
                )
                if response.status_code >= 400:
                    raise DriveArchiveError(
                        f"Google Drive folder verification failed with {response.status_code}"
                    )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise DriveArchiveError(
                        "Google Drive folder verification returned invalid metadata"
                    )
                if payload.get("trashed") is True:
                    raise DriveArchiveError("Configured RivaL Drive folder is trashed")
                if payload.get("mimeType") != "application/vnd.google-apps.folder":
                    raise DriveArchiveError(
                        "Configured RivaL Drive boundary ID is not a folder"
                    )
                if str(payload.get("id") or "") != folder_id:
                    raise DriveArchiveError(
                        "Google Drive folder verification returned unexpected identity"
                    )
                return payload

            root = await folder_metadata(self.folders.root)
            root_parents = root.get("parents") or []
            if not isinstance(root_parents, list):
                raise DriveArchiveError("RivaL Drive root returned invalid parent metadata")

            for child_id in (
                self.folders.history,
                self.folders.manifests,
                self.folders.staging,
            ):
                child = await folder_metadata(child_id)
                parents = child.get("parents") or []
                if not isinstance(parents, list) or self.folders.root not in {
                    str(item) for item in parents
                }:
                    raise DriveArchiveError(
                        "Configured archive folder is outside the RivaL Data Warehouse root"
                    )

            self._boundary_verified = True

    async def _find_batch_object(
        self,
        *,
        parent_id: str,
        batch_id: str,
        kind: str,
    ) -> DriveObject | None:
        allowed = {
            self.folders.history,
            self.folders.manifests,
            self.folders.staging,
        }
        if parent_id not in allowed:
            raise DriveArchiveError("Drive search attempted outside the RivaL archive boundary")

        query = (
            f"'{self._escape_query(parent_id)}' in parents and trashed = false "
            f"and appProperties has {{ key='rival_batch_id' and "
            f"value='{self._escape_query(batch_id)}' }} "
            f"and appProperties has {{ key='rival_kind' and "
            f"value='{self._escape_query(kind)}' }}"
        )
        response = await self._client.get(
            f"{self.DRIVE_URL}/files",
            params={
                "q": query,
                "spaces": "drive",
                "pageSize": 10,
                "fields": "files(id,name,size,md5Checksum,parents)",
            },
            headers=await self._headers(),
        )
        if response.status_code >= 400:
            raise DriveArchiveError(
                f"Google Drive bounded search failed with {response.status_code}"
            )

        payload = response.json()
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            raise DriveArchiveError("Google Drive bounded search returned invalid data")
        if len(files) > 1:
            raise DriveArchiveError(
                f"Multiple Drive objects exist for RivaL batch {batch_id}/{kind}"
            )
        if not files:
            return None
        if not isinstance(files[0], dict):
            raise DriveArchiveError("Google Drive returned invalid file metadata")
        result = self._object(files[0])
        if parent_id not in result.parents:
            raise DriveArchiveError("Drive search result escaped its expected parent")
        return result

    @staticmethod
    def _verify_artifact(obj: DriveObject, artifact: ArchiveArtifact) -> None:
        if obj.size_bytes != artifact.size_bytes:
            raise DriveArchiveError(
                f"Drive size mismatch for {artifact.file_name}: "
                f"local={artifact.size_bytes}, remote={obj.size_bytes}"
            )
        if obj.md5 is None or obj.md5.casefold() != artifact.md5.casefold():
            raise DriveArchiveError(
                f"Drive checksum mismatch for {artifact.file_name}"
            )

    async def _upload_resumable(
        self,
        *,
        artifact: ArchiveArtifact,
        parent_id: str,
        batch_id: str,
        kind: str,
        mime_type: str,
    ) -> DriveObject:
        if parent_id not in {self.folders.staging, self.folders.manifests}:
            raise DriveArchiveError("New Drive uploads must target RivaL staging or manifests")

        existing = await self._find_batch_object(
            parent_id=parent_id,
            batch_id=batch_id,
            kind=kind,
        )
        if existing is not None:
            self._verify_artifact(existing, artifact)
            return existing

        headers = await self._headers()
        headers.update(
            {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(artifact.size_bytes),
            }
        )
        response = await self._client.post(
            f"{self.UPLOAD_URL}/files",
            params={
                "uploadType": "resumable",
                "fields": "id,name,size,md5Checksum,parents",
            },
            json={
                "name": artifact.file_name,
                "parents": [parent_id],
                "appProperties": {
                    "rival_batch_id": batch_id,
                    "rival_kind": kind,
                    "rival_schema_version": str(artifact.schema_version),
                    "rival_sha256": artifact.sha256,
                },
            },
            headers=headers,
        )
        if response.status_code not in {200, 201}:
            raise DriveArchiveError(
                f"Drive resumable upload initiation failed with {response.status_code}"
            )

        session_url = response.headers.get("Location")
        if not session_url:
            raise DriveArchiveError("Drive resumable upload returned no session URL")

        offset = 0
        final_payload: dict[str, Any] | None = None
        with artifact.path.open("rb") as handle:
            while offset < artifact.size_bytes:
                handle.seek(offset)
                chunk = handle.read(
                    min(self._chunk_size, artifact.size_bytes - offset)
                )
                if not chunk:
                    raise DriveArchiveError("Archive file ended before expected size")

                end = offset + len(chunk) - 1
                upload = await self._client.put(
                    session_url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": (
                            f"bytes {offset}-{end}/{artifact.size_bytes}"
                        ),
                    },
                )

                if upload.status_code == 308:
                    received = upload.headers.get("Range")
                    if received and "-" in received:
                        try:
                            offset = int(received.rsplit("-", 1)[1]) + 1
                        except ValueError as exc:
                            raise DriveArchiveError(
                                "Drive resumable upload returned invalid Range header"
                            ) from exc
                    else:
                        offset = end + 1
                    continue

                if upload.status_code not in {200, 201}:
                    raise DriveArchiveError(
                        f"Drive resumable upload failed with {upload.status_code}"
                    )
                parsed = upload.json()
                if not isinstance(parsed, dict):
                    raise DriveArchiveError(
                        "Drive resumable upload returned invalid final metadata"
                    )
                final_payload = parsed
                offset = artifact.size_bytes

        if final_payload is None:
            raise DriveArchiveError("Drive resumable upload finished without metadata")

        result = self._object(final_payload)
        if parent_id not in result.parents:
            raise DriveArchiveError("Uploaded Drive object has unexpected parent")
        self._verify_artifact(result, artifact)
        return result

    async def ensure_history_file(
        self,
        *,
        artifact: ArchiveArtifact,
        batch_id: str,
    ) -> DriveObject:
        await self._verify_folder_boundary()
        existing = await self._find_batch_object(
            parent_id=self.folders.history,
            batch_id=batch_id,
            kind="history",
        )
        if existing is not None:
            self._verify_artifact(existing, artifact)
            return existing

        staged = await self._upload_resumable(
            artifact=artifact,
            parent_id=self.folders.staging,
            batch_id=batch_id,
            kind="history",
            mime_type="application/vnd.apache.parquet",
        )
        if self.folders.staging not in staged.parents:
            raise DriveArchiveError("History object is not in RivaL staging")

        response = await self._client.patch(
            f"{self.DRIVE_URL}/files/{staged.file_id}",
            params={
                "addParents": self.folders.history,
                "removeParents": self.folders.staging,
                "fields": "id,name,size,md5Checksum,parents",
            },
            headers=await self._headers(),
        )
        if response.status_code >= 400:
            raise DriveArchiveError(
                f"Drive history promotion failed with {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise DriveArchiveError("Drive history promotion returned invalid metadata")

        promoted = self._object(payload)
        if (
            self.folders.history not in promoted.parents
            or self.folders.staging in promoted.parents
        ):
            raise DriveArchiveError("Drive history promotion did not preserve boundary")
        self._verify_artifact(promoted, artifact)
        return promoted

    async def ensure_manifest_file(
        self,
        *,
        artifact: ArchiveArtifact,
        batch_id: str,
    ) -> DriveObject:
        await self._verify_folder_boundary()
        return await self._upload_resumable(
            artifact=artifact,
            parent_id=self.folders.manifests,
            batch_id=batch_id,
            kind="manifest",
            mime_type="application/json",
        )
