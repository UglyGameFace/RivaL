"""Private-bot owner gate and Healthchecks.io heartbeat."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
import httpx
import pytest
from discord import app_commands
from discord.ui.modal import Modal
from discord.ui.view import BaseView

from parlay_bot.discord_app import owner_gate
from parlay_bot.discord_app.heartbeat import HealthchecksHeartbeat

OWNER = 111111111111111111
STRANGER = 222222222222222222


class _Response:
    def __init__(self):
        self.sent = []

    def is_done(self):
        return bool(self.sent)

    async def send_message(self, content, *, ephemeral=False):
        self.sent.append((content, ephemeral))


def _interaction(user_id: int):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), response=_Response())


class _Client:
    def __init__(self, info=None, error=None):
        self.info, self.error, self.calls = info, error, 0

    async def application_info(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.info


def test_parse_owner_ids_rejects_non_snowflakes():
    assert owner_gate.parse_owner_ids(f" {OWNER}, {STRANGER} ") == {OWNER, STRANGER}
    assert owner_gate.parse_owner_ids("") == frozenset()
    for bad in ("abc", "123", f"{OWNER},x"):
        with pytest.raises(ValueError):
            owner_gate.parse_owner_ids(bad)


@pytest.mark.asyncio
async def test_configured_ids_skip_discord_lookup():
    client = _Client()
    gate = owner_gate.OwnerGate(client, str(OWNER))
    assert await gate.allows(_interaction(OWNER))
    assert not await gate.allows(_interaction(STRANGER))
    assert client.calls == 0


@pytest.mark.asyncio
async def test_application_owner_or_team_members_are_owners():
    solo = _Client(SimpleNamespace(team=None, owner=SimpleNamespace(id=OWNER)))
    gate = owner_gate.OwnerGate(solo)
    assert await gate.allows(_interaction(OWNER))
    assert not await gate.allows(_interaction(STRANGER))
    assert solo.calls == 1  # cached after first success
    team = SimpleNamespace(members=[SimpleNamespace(id=OWNER), SimpleNamespace(id=STRANGER)])
    teamed = owner_gate.OwnerGate(_Client(SimpleNamespace(team=team, owner=None)))
    assert await teamed.allows(_interaction(STRANGER))


@pytest.mark.asyncio
async def test_lookup_failure_fails_closed_then_retries():
    client = _Client(error=discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x"))
    gate = owner_gate.OwnerGate(client)
    assert not await gate.allows(_interaction(OWNER))
    client.error = None
    client.info = SimpleNamespace(team=None, owner=SimpleNamespace(id=OWNER))
    assert await gate.allows(_interaction(OWNER))
    assert client.calls == 2


def test_discord_dispatch_hooks_have_expected_shape():
    import inspect

    view_params = list(inspect.signature(BaseView._scheduled_task).parameters)
    modal_params = list(inspect.signature(Modal._scheduled_task).parameters)
    # Wrapped or not, the interaction position must match _wrap_dispatch.
    if not getattr(BaseView._scheduled_task, owner_gate._MARKER, False):
        assert view_params[:3] == ["self", "item", "interaction"]
    if not getattr(Modal._scheduled_task, owner_gate._MARKER, False):
        assert modal_params[:2] == ["self", "interaction"]


@pytest.mark.asyncio
async def test_installed_gate_refuses_strangers_everywhere(monkeypatch):
    monkeypatch.setattr(owner_gate, "_gate", None)
    gate = owner_gate.OwnerGate(_Client(), str(OWNER))
    tree = app_commands.CommandTree.__new__(app_commands.CommandTree)
    owner_gate.install_owner_gate(tree, gate)

    stranger = _interaction(STRANGER)
    assert await tree.interaction_check(stranger) is False
    assert stranger.response.sent == [(owner_gate.PRIVATE_NOTICE, True)]
    assert await tree.interaction_check(_interaction(OWNER)) is True

    # View taps: (self, item, interaction). A refused tap never runs the item.
    calls = []

    class Probe(BaseView):
        async def _scheduled_task(self, item, interaction):
            calls.append(interaction.user.id)

    owner_gate._wrap_dispatch(Probe, 1)
    probe = Probe.__new__(Probe)
    tapper = _interaction(STRANGER)
    await probe._scheduled_task(object(), tapper)
    assert calls == []
    assert tapper.response.sent == [(owner_gate.PRIVATE_NOTICE, True)]
    await probe._scheduled_task(object(), _interaction(OWNER))
    assert calls == [OWNER]
    assert getattr(BaseView._scheduled_task, owner_gate._MARKER)
    assert getattr(Modal._scheduled_task, owner_gate._MARKER)


class _Store:
    def __init__(self, fail=False):
        self.fail = fail

    def connect(self):
        store = self

        class Conn:
            def __enter__(self):
                if store.fail:
                    raise RuntimeError("database down")
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, *_args):
                return SimpleNamespace(fetchone=lambda: {"ok": 1})

        return Conn()


@pytest.mark.asyncio
async def test_heartbeat_pings_success_or_fail_without_logging_url(caplog):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    url = "https://hc-ping.com/00000000-0000-0000-0000-000000000000"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = HealthchecksHeartbeat(url, _Store(), client=client)
        assert await ok.beat_once() is True
        bad = HealthchecksHeartbeat(url + "/", _Store(fail=True), client=client)
        with caplog.at_level(logging.WARNING):
            assert await bad.beat_once() is False
    assert seen == [url, url + "/fail"]
    assert "hc-ping.com" not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_survives_ping_errors_and_requires_https(caplog):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(ValueError):
        HealthchecksHeartbeat("http://hc-ping.com/x", _Store())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        beat = HealthchecksHeartbeat("https://hc-ping.com/abc", _Store(), client=client)
        with caplog.at_level(logging.WARNING):
            assert await beat.beat_once() is True
    assert "ConnectError" in caplog.text
    assert "hc-ping.com" not in caplog.text
