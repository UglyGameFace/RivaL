"""Independent host monitoring is fail-closed and never restarts the app."""
from __future__ import annotations

import pytest

from tools import discloud_uptime as m


@pytest.mark.parametrize(("container", "expected"), [
    ("Online", "ONLINE"), ("online", "ONLINE"),
    ("Offline", "OFFLINE"), (" offline ", "OFFLINE"),
])
def test_off_host_discloud_status_is_anchored_to_actual_app(container, expected):
    assert m.interpret_status({
        "status": "ok", "apps": {"id": "12345", "container": container},
    }, "12345") == expected


@pytest.mark.parametrize("bad", [
    {}, {"status": "error", "apps": {"id": "12345", "container": "Online"}},
    {"status": "ok", "apps": {"id": "wrong", "container": "Online"}},
    {"status": "ok", "apps": {"id": "12345", "container": "Starting"}},
    {"status": "ok", "apps": [{"id": "12345", "container": "Online"}]},
    {"status": "ok", "apps": {"id": "12345"}},
])
def test_unknown_or_mismatched_discloud_is_unverified_not_offline(bad):
    with pytest.raises(m.StatusUnverified):
        m.interpret_status(bad, "12345")


def test_probe_requires_two_consistent_observations(monkeypatch):
    states = iter(("Online", "Offline"))
    events = []

    def fake_json(url, **kwargs):
        assert url == "https://api.discloud.app/v2/app/12345/status"
        assert kwargs["token"] == "token"
        return {"status": "ok", "apps": {
            "id": "12345", "container": next(states),
        }}

    monkeypatch.setattr(m, "api_json", fake_json)
    monkeypatch.setattr(m.time, "sleep", lambda pause: events.append(pause))
    with pytest.raises(m.StatusUnverified, match="changed"):
        m.sample_twice("12345", "token")
    assert events == [5]


def test_probe_never_claims_health_on_missing_token():
    with pytest.raises(m.StatusUnverified):
        m.sample_twice("12345", "")
    with pytest.raises(m.StatusUnverified):
        m.sample_twice("../../unsafe", "token")


def test_github_incident_opens_once_and_closes_on_recovery(monkeypatch):
    issues = []
    writes = []

    def fake_json(url, *, token, method="GET", payload=None):
        assert token == "gh-token"
        if method == "GET":
            assert "?state=all&per_page=100&page=1" in url
            return issues.copy()
        writes.append((url, method, payload))
        if method == "POST" and url.endswith("/issues"):
            issues.append({"number": 99, "title": m.DISCORD_APP_STATE_ISSUE,
                           "state": "open"})
        elif method == "PATCH":
            issues[0]["state"] = payload["state"]
        return {"status": "ok"}

    monkeypatch.setattr(m, "api_json", fake_json)
    opts = {"repository": "UglyGameFace/RivaL", "token": "gh-token"}
    assert m.synchronize_incident("OFFLINE", **opts) == "opened"
    assert m.synchronize_incident("OFFLINE", **opts) == "already_open"
    assert m.synchronize_incident("ONLINE", **opts) == "closed"
    assert m.synchronize_incident("ONLINE", **opts) == "no_active_incident"
    assert m.synchronize_incident("OFFLINE", **opts) == "reopened"
    assert [call[1] for call in writes] == ["POST", "POST", "PATCH", "PATCH"]
    assert not any("restart" in call[0] for call in writes)


def test_github_incident_fails_closed_when_history_is_ambiguous(monkeypatch):
    monkeypatch.setattr(m, "api_json", lambda *a, **kw: [
        {"number": 1, "state": "open", "title": m.DISCORD_APP_STATE_ISSUE},
        {"number": 2, "state": "closed", "title": m.DISCORD_APP_STATE_ISSUE},
    ])
    with pytest.raises(m.StatusUnverified, match="duplicate"):
        m.synchronize_incident("OFFLINE", repository="UglyGameFace/RivaL", token="gh")
