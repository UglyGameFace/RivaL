"""Check model inventory, SHA verification, atomic downloads, and no data overwrite."""
from __future__ import annotations

import hashlib
import io
import json
import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "services" / "rival-llm" / "model_download.py"
))
# runpy function objects retain module globals even when not registered.
assert "ensure_model" in MODULE


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_official_lfs_manifest_must_have_sha256(monkeypatch):
    data = b"model-file" * 3
    digest = hashlib.sha256(data).hexdigest()
    fn = MODULE["_fetch_manifest"]
    fn.__globals__["MIN_MODEL_BYTES"] = 5
    fn.__globals__["MAX_MODEL_BYTES"] = 300

    def fake_fetch(request, *, timeout):
        assert "huggingface.co/api/models/" in request.full_url
        return Response(json.dumps([{"type": "file", "path": "sample.gguf",
                                      "lfs": {"oid": digest, "size": len(data)}}]).encode())
    monkeypatch.setattr(fn.__globals__["urllib"].request, "urlopen", fake_fetch)
    assert fn("LiquidAI/LFM3-1.7B-GGUF", "sample.gguf", "main") == (
        digest, len(data))
    with pytest.raises(ValueError, match="revision"):
        fn("LiquidAI/LFM3-1.7B-GGUF", "sample.gguf", "../../evil")


def test_verified_model_atomic_download_and_reuse(tmp_path, monkeypatch):
    raw = b"all-good-test-bytes"
    digest = hashlib.sha256(raw).hexdigest()
    ensure = MODULE["ensure_model"]
    lookup = ensure.__globals__["_fetch_manifest"]
    monkeypatch.setitem(ensure.__globals__, "_fetch_manifest",
                        lambda *_args: (digest, len(raw)))
    monkeypatch.setenv("RIVAL_LLM_MODEL_DIR", str(tmp_path))

    calls = []
    def fake_download(req, *, timeout):
        calls.append(req.full_url)
        return Response(raw)
    monkeypatch.setattr(ensure.__globals__["urllib"].request, "urlopen",
                        fake_download)
    p = ensure(repo="LiquidAI/LFM3-1.7B-GGUF",
               filename="LFM3-1.7B-Q4_K_M.gguf", profile="lfm3")
    assert p.read_bytes() == raw
    assert ".part" not in " ".join(x.name for x in tmp_path.iterdir())
    assert ensure(repo="LiquidAI/LFM3-1.7B-GGUF",
                  filename="LFM3-1.7B-Q4_K_M.gguf", profile="lfm3") == p
    assert len(calls) == 1
    p.write_bytes(b"invalid-archive")
    with pytest.raises(ValueError, match="failed SHA256"):
        ensure(repo="LiquidAI/LFM3-1.7B-GGUF",
                     filename="LFM3-1.7B-Q4_K_M.gguf", profile="lfm3")
    assert p.read_bytes() == b"invalid-archive"


def test_partial_or_wrong_hash_is_deleted(tmp_path, monkeypatch):
    ensure = MODULE["ensure_model"]
    monkeypatch.setenv("RIVAL_LLM_MODEL_DIR", str(tmp_path))
    monkeypatch.setitem(ensure.__globals__, "_fetch_manifest",
                        lambda *_args: ("0" * 64, 13))
    monkeypatch.setattr(ensure.__globals__["urllib"].request, "urlopen",
                        lambda _request, *, timeout: Response(b"short"))
    with pytest.raises(ValueError, match="did not match"):
        ensure(repo="LiquidAI/LFM3-1.7B-GGUF",
               filename="LFM3-1.7B-Q4_K_M.gguf", profile="lfm3")
    assert list(tmp_path.iterdir()) == []
