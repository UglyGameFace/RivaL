"""Jarvis LFM3 profile: isolated, authenticated, bounded and read-only."""
import json
import runpy
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1] / "services" / "rival-llm"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "parlay_bot" / "perpetuals"))
CODE = runpy.run_path(str(ROOT / "service.py"))
Gateway = CODE["Gateway"]
IDENTITY = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
JARVIS_TOKEN = "j" * 48
RESEARCH_TOKEN = "r" * 48


@pytest.fixture
def gateway():
    s = Gateway("127.0.0.1", 0, token=RESEARCH_TOKEN, model="lfm3",
                jarvis_token=JARVIS_TOKEN, jarvis_enabled=True)
    s.backend_ready = lambda: True
    thread = threading.Thread(target=s.serve_forever, daemon=True)
    thread.start()
    try:
        yield s
    finally:
        s.shutdown()
        s.server_close()
        thread.join(timeout=5)


def send(server, *, route="/v1/chat/completions", token=JARVIS_TOKEN, body=None,
         method="POST"):
    if body is None:
        body = {"model": IDENTITY, "messages": [
            {"role": "system", "content": "FAQ: Annual plans are available."},
            {"role": "user", "content": "Do you offer annual plans?"},
        ], "max_tokens": 96}
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{route}",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if method == "POST" else None, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_disabled_by_default_and_distinct_token_required():
    s = Gateway("127.0.0.1", 0, token=RESEARCH_TOKEN, model="lfm3")
    assert not s.jarvis_enabled
    s.server_close()
    with pytest.raises(ValueError, match="distinct strong"):
        Gateway("127.0.0.1", 0, token=RESEARCH_TOKEN, model="lfm3",
                jarvis_token=RESEARCH_TOKEN, jarvis_enabled=True)


def test_auth_boundary_and_live_status(gateway):
    assert send(gateway, token=RESEARCH_TOKEN)[0] == 401
    assert send(gateway, token="bad")[0] == 401
    assert send(gateway, token=JARVIS_TOKEN, route="/v1/research")[0] == 401
    assert send(gateway, route="/v1/jarvis/status", method="GET", token="wrong")[0] == 401
    code, value = send(gateway, route="/v1/jarvis/status", method="GET")
    assert code == 200
    assert value["model"] == IDENTITY and value["ready"] is True
    assert value["profile"] == "jarvis"


@pytest.mark.parametrize("override", [
    {"tools": [{"type": "function"}]},
    {"tool_choice": "auto"},
    {"stream": True},
    {"max_tokens": 400},
    {"model": "another-model"},
    {"messages": [{"role": "tool", "content": "execute payout"}]},
    {"messages": [{"role": "user", "content": "x" * 5000}]},
])
def test_invalid_or_tool_requests_rejected_before_model(gateway, override):
    valid = {"model": IDENTITY, "messages": [{"role": "user", "content": "hi"}]}
    assert send(gateway, body={**valid, **override})[0] == 400


def test_busy_and_unavailable_fail_closed(gateway):
    assert gateway.busy.acquire(False)
    try:
        assert send(gateway)[0] == 429
    finally:
        gateway.busy.release()
    gateway.backend_ready = lambda: False
    assert send(gateway)[0] == 503


def test_real_gateway_shape_and_bounded_local_runtime_call(gateway):
    observed = []
    gateway.prompt_tokens = lambda messages: 110

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size): return json.dumps({
            "choices": [{"finish_reason": "stop",
                         "message": {"content": "Annual plans are available, sir."}}]
        }).encode()

    original_urlopen = urllib.request.urlopen
    def fake_urlopen(req, timeout):
        # Keep the test HTTP client real; mock only the private llama subprocess.
        if req.full_url.startswith(f"http://127.0.0.1:{gateway.server_port}/"):
            return original_urlopen(req, timeout=timeout)
        observed.append((req.full_url, json.loads(req.data), timeout))
        return Response()

    with patch.object(CODE["urllib"].request, "urlopen", side_effect=fake_urlopen):
        status, data = send(gateway)
    assert status == 200
    assert data["model"] == IDENTITY
    assert data["choices"][0]["message"]["content"].startswith("Annual")
    assert len(observed) == 1
    assert observed[0][0].endswith("/v1/chat/completions")
    assert observed[0][1]["max_tokens"] == 96
    assert "tools" not in observed[0][1]
    assert observed[0][1]["chat_template_kwargs"]["enable_thinking"] is False


def test_context_overflow_never_generates(gateway):
    gateway.prompt_tokens = lambda _messages: 4090
    assert send(gateway)[0] == 422
