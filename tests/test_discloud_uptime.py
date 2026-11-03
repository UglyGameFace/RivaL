"""Uptime monitoring regression checks: malformed GitHub reads never cause false incidents."""
import json
from unittest.mock import Mock

import pytest

from tools import discloud_uptime as uptime


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _limit):
        return self.payload


def test_github_malformed_get_retries_and_recovers(monkeypatch):
    good = {"total_count": 0, "incomplete_results": False, "items": []}
    opener = Mock(side_effect=[Response(b"<not-json>"), Response(json.dumps(good).encode())])
    monkeypatch.setattr(uptime.request, "urlopen", opener)
    monkeypatch.setattr(uptime.time, "sleep", lambda _: None)
    result = uptime.api_json("https://api.github.com/search/issues?q=uptime", token="token")
    assert result == good
    assert opener.call_count == 2
    req = opener.call_args.args[0]
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert req.get_header("X-github-api-version") == "2022-11-28"


def test_github_persistent_malformed_get_is_unverified_not_offline(monkeypatch):
    opener = Mock(return_value=Response(b"<not-json>"))
    monkeypatch.setattr(uptime.request, "urlopen", opener)
    monkeypatch.setattr(uptime.time, "sleep", lambda _: None)
    with pytest.raises(uptime.StatusUnverified, match="GitHub incident API response malformed"):
        uptime.api_json("https://api.github.com/search/issues?q=uptime", token="token")
    assert opener.call_count == 3


def test_malformed_incident_write_is_never_retried(monkeypatch):
    opener = Mock(return_value=Response(b"not-json"))
    monkeypatch.setattr(uptime.request, "urlopen", opener)
    monkeypatch.setattr(uptime.time, "sleep", lambda _: None)
    with pytest.raises(uptime.StatusUnverified, match="GitHub incident API response malformed"):
        uptime.api_json("https://api.github.com/repos/UglyGameFace/RivaL/issues",
                        token="token", method="POST", payload={"title": "test"})
    assert opener.call_count == 1


def test_online_with_verified_empty_issue_search_creates_no_incident(monkeypatch):
    calls = []
    def fake_api(url, *, token, method="GET", payload=None):
        calls.append((url, method, payload))
        return {"total_count": 0, "incomplete_results": False, "items": []}
    monkeypatch.setattr(uptime, "api_json", fake_api)
    assert uptime.synchronize_incident("ONLINE", repository="UglyGameFace/RivaL",
                                       token="token") == "no_active_incident"
    assert len(calls) == 1
    assert calls[0][1] == "GET"
