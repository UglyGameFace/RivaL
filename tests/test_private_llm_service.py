"""Bounded isolated HTTP gateway: no market DB, no public inference, no trades."""
from __future__ import annotations

import json
import runpy
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVICE = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "services" / "rival-llm" / "service.py"
))
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
    assert send(server, body={"asset": "XRP", "evidence": {}})[0] == 400
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
