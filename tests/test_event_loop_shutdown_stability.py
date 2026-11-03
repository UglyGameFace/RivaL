"""Shutdown, error-notice and background-task safety for the Discord client."""
from __future__ import annotations

import asyncio
import gc

import pytest
from discord import app_commands

from parlay_bot.config import Settings
from parlay_bot.discord_app import home
from parlay_bot.discord_app.bot import RivalDiscordClient


def _client(tmp_path) -> RivalDiscordClient:
    return RivalDiscordClient(Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
        rival_archive_autostart_when_configured=False,
    ))


@pytest.mark.asyncio
async def test_close_cancels_workers_and_survives_a_failing_client(tmp_path):
    client = _client(tmp_path)
    closed: list[str] = []

    async def boom():
        raise RuntimeError("simulated close failure")

    async def zip_close():
        closed.append("zip")

    async def forever():
        await asyncio.Event().wait()

    worker = asyncio.create_task(forever())
    client._sports_task = worker
    client.kalshi_browser.aclose = boom  # first closeable fails
    client.zip_resolver.aclose = zip_close  # last closeable must still run
    await client.close()
    assert worker.cancelled()
    assert client._sports_task is None
    assert closed == ["zip"]


class _Response:
    def __init__(self, done: bool):
        self.done = done
        self.sent: list[tuple[str, bool]] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content, *, ephemeral=False):
        self.sent.append((content, ephemeral))


class _Interaction:
    command = None

    def __init__(self, done: bool):
        self.response = _Response(done)
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


@pytest.mark.asyncio
async def test_command_error_never_leaves_user_thinking(tmp_path):
    client = _client(tmp_path)
    try:
        assert client.tree.on_error == client._on_app_command_error
        fresh = _Interaction(done=False)
        await client._on_app_command_error(fresh, app_commands.AppCommandError("x"))
        assert fresh.response.sent and fresh.response.sent[0][1] is True
        deferred = _Interaction(done=True)
        await client._on_app_command_error(deferred, app_commands.AppCommandError("x"))
        assert "went wrong" in deferred.edits[0]["content"]
        assert deferred.edits[0]["view"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_spawned_menu_task_is_held_until_done():
    finished = asyncio.Event()

    async def work():
        await asyncio.sleep(0.01)
        finished.set()

    task = home._spawn(work(), name="test-menu-task")
    del task
    gc.collect()
    assert len(home._BACKGROUND_TASKS) >= 1
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not any(t.get_name() == "test-menu-task" for t in home._BACKGROUND_TASKS)
