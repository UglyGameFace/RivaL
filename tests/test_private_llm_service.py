"""Bounded isolated HTTP gateway: no market DB, no public inference, no trades."""
from __future__ import annotations

import json
import runpy
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "services" / "rival-llm"
sys.path.insert(0, str(ROOT))
SERVICE = runpy.run_path(str(ROOT / "service.py"))
Gateway = SERVICE["Gateway"]


@pytest.fixture
def gateway():
    server = Gateway(
        "127.0.0.1", 0, token="t" * 48, model="lfm3",
        backend_url="http://127.0.0.1:99999",
    )
    server.backend_ready = lambda: True
    calls = []
    def infer(asset, evidence):
        calls.append((asset, evidence))
        return {"model": server.identity, "direction": "uncertain",
                "confidence": "low", "thesis": "No edge verified",
                "risk": "Quotes missing", "uncalibrated": True,
                "informational_only": True}
    server.infer = infer
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def send(server, *, route="/v1/research", method="POST", token="t" * 48,
         body=None, content_type="application/json"):
    payload = json.dumps(body if body is not None else {
        "asset": "BTC", "evidence": {"sources": [{"venue": "okx"}]}
    }).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{route}",
        data=payload if method == "POST" else None, method=method,
        headers={"Content-Type": content_type,
                 "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_no_unauthorized_or_malformed_inference(gateway):
    server, calls = gateway
    assert send(server, token="wrong")[0] == 401
    assert send(server, token="wrong", route="/v1/status", method="GET")[0] == 401
    assert send(server, body={"asset": "BTC", "evidence": []})[0] == 400
    assert send(server, body={"asset": "DOGE", "evidence": {}})[0] == 400
    assert send(server, body={"asset": "BTC", "evidence": {}},
                content_type="text/plain")[0] == 415
    assert send(server, body={"asset": "BTC", "evidence": {"data": "x" * 30_000}})[0] == 413
    assert calls == []


def test_one_authenticated_private_research_request(gateway):
    server, calls = gateway
    status, result = send(server)
    assert status == 200
    assert result["model"] == "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
    assert result["informational_only"] and result["uncalibrated"]
    assert calls == [("BTC", {"sources": [{"venue": "okx"}]})]
    assert send(server, route="/health", method="GET")[1]["status"] == "ready"


def test_xrp_supported_without_weakening_authentication(gateway):
    server, calls = gateway
    status, result = send(
        server, body={"asset": "XRP", "evidence": {"sources": [{"venue": "gate"}]}},
    )
    assert status == 200
    assert result["informational_only"] is True
    assert calls == [("XRP", {"sources": [{"venue": "gate"}]})]


def test_busy_and_backend_missing_fail_closed(gateway):
    server, calls = gateway
    assert server.busy.acquire(False)
    try:
        assert send(server)[0] == 429
    finally:
        server.busy.release()
    server.backend_ready = lambda: False
    assert send(server)[0] == 503
    assert calls == []


def test_no_model_runtime_or_weak_credentials():
    with pytest.raises(ValueError, match="32 characters"):
        Gateway("127.0.0.1", 0, token="too-short", model="lfm3")
    with pytest.raises(ValueError, match="Unrecognized"):
        Gateway("127.0.0.1", 0, token="x" * 40, model="not-a-model")


def test_model_output_validation():
    valid = {"direction": "uncertain", "confidence": "low",
             "thesis": "Insufficient data", "risk": "Unknown fees"}
    assert SERVICE["validated_research"](valid)
    assert not SERVICE["validated_research"]({**valid, "direction": "guaranteed"})
    assert not SERVICE["validated_research"]({**valid, "risk": "x" * 251})
    assert not SERVICE["validated_research"]("anything")


def test_select_only_allowlisted_quantized_model():
    assert sorted(SERVICE["MODEL_CATALOG"]) == ["lfm3", "qwen2b"]
    assert all(file.endswith(".gguf") for _, file in SERVICE["MODEL_CATALOG"].values())

def test_context_overflow_returns_non_fallback_422(gateway):
    server, calls = gateway
    def too_large(_asset, _evidence):
        raise SERVICE["ContextBudgetExceeded"]("would truncate")
    server.infer = too_large
    status, result = send(server)
    assert status == 422
    assert result == {"error": "context_budget_exceeded"}
    assert calls == []


def test_prompt_preflight_uses_rendered_chat_template_and_real_tokenizer(monkeypatch):
    server = Gateway("127.0.0.1", 0, token="t" * 48, model="lfm3")
    observed = []
    class FakeResponse:
        def __init__(self, data):
            self.payload = json.dumps(data).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.payload

    def fake_urlopen(request, timeout):
        observed.append((request.full_url, json.loads(request.data), timeout))
        if request.full_url.endswith("/apply-template"):
            return FakeResponse({"prompt": "Rendered chat with special tokens"})
        if request.full_url.endswith("/tokenize"):
            return FakeResponse({"tokens": [7] * 3800})
        raise AssertionError("oversized prompt must never start generation")

    try:
        monkeypatch.setattr(SERVICE["urllib"].request, "urlopen", fake_urlopen)
        with pytest.raises(SERVICE["ContextBudgetExceeded"], match="context window"):
            server.infer("BTC", {"sources": [{"venue": "okx"}]})
        assert [url.rsplit("/", 1)[-1] for url, _, _ in observed] == [
            "apply-template", "tokenize"
        ]
        assert observed[0][1]["messages"][0]["role"] == "system"
        assert observed[1][1]["content"] == "Rendered chat with special tokens"
    finally:
        server.server_close()

def test_prompt_preflight_allows_complete_compatible_requests(monkeypatch):
    server = Gateway("127.0.0.1", 0, token="t" * 48, model="lfm3")
    calls = []
    class FakeResponse:
        status = 200

        def __init__(self, data):
            self.data = json.dumps(data).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.data

    def fake_urlopen(request, timeout):
        path = request.full_url.rsplit("/", 1)[-1]
        calls.append((path, json.loads(request.data)))
        if path == "apply-template":
            return FakeResponse({"prompt": "model-rendered prompt"})
        if path == "tokenize":
            return FakeResponse({"tokens": [11] * 200})
        if path == "completions":
            return FakeResponse({"choices": [{"message": {"content": json.dumps({
                "direction": "uncertain", "confidence": "low",
                "thesis": "Insufficient forward evidence", "risk": "No matched quotes"
            })}}]})
        raise AssertionError("unexpected outbound local service path")

    try:
        monkeypatch.setattr(SERVICE["urllib"].request, "urlopen", fake_urlopen)
        result = server.infer("BTC", {"sources": [{"venue": "okx"}]})
        assert result["model"] == "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
        assert result["informational_only"] is True
        assert [path for path, _ in calls] == [
            "apply-template", "tokenize", "completions"
        ]
        assert calls[-1][1]["max_tokens"] == 300
    finally:
        server.server_close()
