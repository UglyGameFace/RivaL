"""Stream and verify a fixed public GGUF from the official Hugging Face file tree.

Never imports model weights into Python, never loads remote executable code.
A corrupt/partial file is not promoted to the durable model path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MIN_MODEL_BYTES = 500_000_000
MAX_MODEL_BYTES = 2_300_000_000
REVISION = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _fetch_manifest(repo: str, filename: str, revision: str) -> tuple[str, int]:
    if not REVISION.fullmatch(revision):
        raise ValueError("Model revision is invalid")
    url = f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=false"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=25) as response:
        rows = json.loads(response.read(300_000))
    if not isinstance(rows, list):
        raise ValueError("Hugging Face metadata is invalid")
    matches = [x for x in rows if isinstance(x, dict)
               and x.get("type") == "file" and x.get("path") == filename]
    if len(matches) != 1:
        raise ValueError("Exact GGUF not advertised at selected revision")
    file = matches[0]
    lfs = file.get("lfs")
    if not isinstance(lfs, dict):
        raise ValueError("GGUF must have a documented LFS digest")
    digest = lfs.get("oid", "")
    size = lfs.get("size", file.get("size", 0))
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ValueError("No verified SHA-256 for source GGUF")
    if not isinstance(size, int) or not MIN_MODEL_BYTES <= size <= MAX_MODEL_BYTES:
        raise ValueError("GGUF is outside expected resource budget")
    return digest, size


def _sha256(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            hash_.update(chunk)
    return hash_.hexdigest()


def ensure_model(*, repo: str, filename: str, profile: str) -> Path:
    revision = os.environ.get("RIVAL_MODEL_REVISION", "main")
    expected, size = _fetch_manifest(repo, filename, revision)
    explicit_sha = os.environ.get("RIVAL_MODEL_SHA256", "").strip().lower()
    if explicit_sha and (not SHA256.fullmatch(explicit_sha) or explicit_sha != expected):
        raise ValueError("Pinned model digest differs from official LFS manifest")
    root = Path(os.environ.get("RIVAL_LLM_MODEL_DIR", "runtime/models")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{profile}-{filename}"
    if destination.exists():
        if destination.stat().st_size == size and _sha256(destination) == expected:
            return destination
        raise ValueError(
            "Existing model file failed SHA256 or size verification; "
            "refusing to overwrite unverified user data"
        )
    # Write only to a separate temporary path and use atomic rename on success.
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"
    part = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".gguf-", suffix=".part",
                                         dir=root, delete=False) as target:
            part = Path(target.name)
            digest = hashlib.sha256()
            received = 0
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "rival-private-model/1"}),
                timeout=90,
            ) as response:
                while chunk := response.read(1 << 20):
                    received += len(chunk)
                    if received > size or received > MAX_MODEL_BYTES:
                        raise ValueError("Source exceeded verified GGUF size")
                    target.write(chunk)
                    digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if received != size or digest.hexdigest() != expected:
            raise ValueError("GGUF download did not match official LFS SHA256")
        part.replace(destination)
        return destination
    finally:
        if part is not None and part.exists():
            part.unlink()
