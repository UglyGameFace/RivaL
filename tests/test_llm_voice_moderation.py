"""Private voice-classifier contract regressions; no Discord or model required."""
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
sys.path.insert(0, str(ROOT.parents[1] / "src" / "parlay_bot" / "perpetuals"))
SERVICE = runpy.run_path(str(ROOT / "service.py"))
from voice_moderation import validate_request, validate_result  # noqa: E402

VOICE = {"version": 1, "transcript": "Please stop insulting me.", "context": []}


def post(server, *, route="/v1/moderate", token="v" * 48, body=None):
    payload = json.dumps(VOICE if body is None else body).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:" + str(server.server_port) + route,
        data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def server():
    gateway = SERVICE["Gateway"](
        "127.0.0.1", 0, model="lfm3", token="t" * 48,
        voice_token="v" * 48, voice_enabled=True,
    )
    gateway.backend_ready = lambda: True
    calls = []

    def classify(body):
        calls.append(body)
        return {"model": gateway.identity, "decision": "review",
                "category": "targeted_harassment", "confidence": "low",
                "advisory_only": True, "calibrated": False,
                "enforcement_authorized": False}

    gateway.infer_voice_moderation = classify
    runner = threading.Thread(target=gateway.serve_forever, daemon=True)
    runner.start()
    try:
        yield gateway, calls
    finally:
        gateway.shutdown()
        gateway.server_close()
        runner.join(timeout=5)


def test_scope_isolated_and_default_off(server):
    instance, calls = server
    assert post(instance, token="t" * 48)[0] == 401
    assert post(instance, token="nope")[0] == 401
    assert post(instance, route="/v1/research")[0] == 401
    assert post(instance)[0] == 200
    assert calls == [VOICE]
    instance.voice_enabled = False
    assert post(instance)[0] == 503
    assert calls == [VOICE]
    with pytest.raises(ValueError, match="distinct"):
        SERVICE["Gateway"](
            "127.0.0.1", 0, token="t" * 48,
            voice_token="t" * 48, voice_enabled=True, model="lfm3",
        )


def test_refuse_unbounded_private_audio_and_malformed_json(server):
    instance, calls = server
    bad = (
        {"version": 1, "transcript": "x" * 1201, "context": []},
        {"version": 1, "transcript": "hello", "context": ["x"] * 3},
        {"version": 1, "transcript": "hello", "context": [], "user_id": 123},
        {"version": True, "transcript": "hello", "context": []},
        {"version": 1, "transcript": "hello", "context": ["x" * 301]},
        {"version": 1, "transcript": "hello" + chr(0), "context": []},
        {"version": 1, "transcript": "hello", "context": "hi"},
    )
    for body in bad:
        assert post(instance, body=body)[0] == 400
    assert calls == []


def test_busy_and_missing_model_never_accumulate_audio(server):
    instance, calls = server
    assert instance.busy.acquire(blocking=False)
    try:
        assert post(instance)[0] == 429
    finally:
        instance.busy.release()
    instance.backend_ready = lambda: False
    assert post(instance)[0] == 503
    assert calls == []


def test_advisory_result_categories_and_identity_protection_contract():
    assert validate_request(VOICE) == VOICE
    assert validate_result({
        "decision": "allow", "category": "none", "confidence": "medium"
    })["enforcement_authorized"] is False
    for result in (
        {"decision": "review", "category": "none", "confidence": "high"},
        {"decision": "allow", "category": "threat", "confidence": "low"},
        {"decision": "review", "category": "threat", "confidence": 0.99},
        {"decision": "review", "category": "threat", "confidence": "high", "quote": "private"},
    ):
        with pytest.raises(ValueError):
            validate_result(result)
    assert "LGBTQ+" in SERVICE["voice_moderation"].PROMPT
    assert "UNTRUSTED" in SERVICE["voice_moderation"].PROMPT


def test_model_uses_same_llm_with_bounded_json_no_generation_on_context_overflow(monkeypatch):
    gateway = SERVICE["Gateway"](
        "127.0.0.1", 0, token="t" * 48, model="lfm3",
        voice_token="v" * 48, voice_enabled=True,
    )
    seen = []

    class Response:
        status = 200
        def __init__(self, body):
            self.body = json.dumps(body).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, *_args):
            return self.body

    def respond(req, timeout):
        seen.append((req.full_url, json.loads(req.data), timeout))
        return Response({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({
                "decision": "review", "category": "unclear", "confidence": "low"
            })
        }}]})

    try:
        gateway.prompt_tokens = lambda _messages: 120
        monkeypatch.setattr(SERVICE["urllib"].request, "urlopen", respond)
        result = gateway.infer_voice_moderation(VOICE)
        assert result["decision"] == "review"
        assert result["advisory_only"] and not result["calibrated"]
        assert result["enforcement_authorized"] is False
        assert len(seen) == 1 and seen[0][0].endswith("/v1/chat/completions")
        payload = seen[0][1]
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert payload["max_tokens"] == 128
        gateway.prompt_tokens = lambda _messages: 4000
        with pytest.raises(SERVICE["ContextBudgetExceeded"]):
            gateway.infer_voice_moderation(VOICE)
        assert len(seen) == 1
    finally:
        gateway.server_close()


def test_invalid_model_output_never_returns_success(monkeypatch):
    gateway = SERVICE["Gateway"](
        "127.0.0.1", 0, token="t" * 48, model="lfm3",
        voice_token="v" * 48, voice_enabled=True,
    )
    gateway.prompt_tokens = lambda _: 100

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, *_args):
            return json.dumps({"choices": [{"message": {"content": json.dumps({
                "decision": "review", "category": "none", "confidence": "high"
            })}}]}).encode()

    try:
        monkeypatch.setattr(SERVICE["urllib"].request, "urlopen", lambda *_args, **_kwargs: Response())
        with pytest.raises(ValueError, match="inconsistent"):
            gateway.infer_voice_moderation(VOICE)
    finally:
        gateway.server_close()
