"""RIVAL-N: statement timeouts must be shorter than the waits that abandon them.

A1: the watchdog health check runs its statements under a 12 s bound, scoped
narrowly so the one-time acceptance join spawned beside it (~20 s) does not
inherit it. A2: the interactive bound is 8 s, under the 10 s dashboard render
wait and the 9 s forecast render wait.
"""
from __future__ import annotations

import asyncio
import contextvars
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.storage import query_scope
from parlay_bot.storage.query_scope import (
    INTERACTIVE_STATEMENT_TIMEOUT_MS,
    WATCHDOG_STATEMENT_TIMEOUT_MS,
)


def test_timeout_values():
    assert INTERACTIVE_STATEMENT_TIMEOUT_MS == 8_000
    assert WATCHDOG_STATEMENT_TIMEOUT_MS == 12_000
    # Every watchdog wait_for is 15 s; the bound must sit under it.
    assert WATCHDOG_STATEMENT_TIMEOUT_MS < 15_000


def test_statement_timeout_sets_and_restores():
    def run():
        assert query_scope.current_statement_timeout_ms() is None
        with query_scope.statement_timeout(12_000):
            assert query_scope.current_statement_timeout_ms() == 12_000
        assert query_scope.current_statement_timeout_ms() is None
        query_scope.mark_interactive()
        with query_scope.statement_timeout(12_000):
            assert query_scope.current_statement_timeout_ms() == 12_000
        assert (
            query_scope.current_statement_timeout_ms()
            == INTERACTIVE_STATEMENT_TIMEOUT_MS
        )

    contextvars.Context().run(run)


def test_statement_timeout_rejects_bad_values():
    def run_bad(bad):
        with query_scope.statement_timeout(bad):
            pass

    for bad in (0, 99, 600_001):
        with pytest.raises(ValueError):
            contextvars.Context().run(run_bad, bad)


def test_statement_timeout_restores_on_exception_and_nests():
    def run():
        with query_scope.statement_timeout(12_000):
            with query_scope.statement_timeout(8_000):
                assert query_scope.current_statement_timeout_ms() == 8_000
            assert query_scope.current_statement_timeout_ms() == 12_000
            try:
                with query_scope.statement_timeout(8_000):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert query_scope.current_statement_timeout_ms() == 12_000
        assert query_scope.current_statement_timeout_ms() is None

    contextvars.Context().run(run)


@pytest.mark.asyncio
async def test_statement_timeout_follows_threads_but_not_later_tasks():
    seen_inside: list[int | None] = []
    seen_spawned_inside: list[int | None] = []
    seen_spawned_after: list[int | None] = []

    async def spawned_inside():
        seen_spawned_inside.append(query_scope.current_statement_timeout_ms())

    async def spawned_after():
        seen_spawned_after.append(query_scope.current_statement_timeout_ms())

    with query_scope.statement_timeout(12_000):
        seen_inside.append(
            await asyncio.to_thread(query_scope.current_statement_timeout_ms)
        )
        await asyncio.create_task(spawned_inside())
    await asyncio.create_task(spawned_after())
    assert seen_inside == [12_000]
    assert seen_spawned_inside == [12_000]
    assert seen_spawned_after == [None]


def _client(tmp_path) -> RivalDiscordClient:
    settings = Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
        rival_llm_background_opt_in=False,
    )
    return RivalDiscordClient(settings)


async def _stop(task: asyncio.Task) -> None:
    """Cancel ``task`` until it actually finishes."""
    while not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=1)
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_watchdog_health_check_runs_under_twelve_seconds(tmp_path):
    client = _client(tmp_path)
    publish_seen = asyncio.Event()
    acceptance_seen = asyncio.Event()
    seen: dict[str, list] = {
        "inspect": [], "settle": [], "reconcile": [], "publish": [], "acceptance": [],
    }
    loop = asyncio.get_running_loop()

    def record(key):
        # Runs in a worker thread (asyncio.to_thread): read the copied context.
        seen[key].append(query_scope.current_statement_timeout_ms())

    def fake_inspect(now, **kwargs):
        record("inspect")
        return {"settlement_backlog": "1 recoverable prospective outcomes"}

    def fake_settle(now):
        record("settle")
        return 1

    def fake_reconcile(now, limit=120):
        record("reconcile")
        return 0

    def fake_publish(faults, now=None):
        record("publish")
        loop.call_soon_threadsafe(publish_seen.set)
        return []

    async def fake_acceptance(now):
        seen["acceptance"].append(query_scope.current_statement_timeout_ms())
        acceptance_seen.set()

    client.health_monitor.inspect = fake_inspect  # type: ignore[method-assign]
    client.health_monitor.publish = fake_publish  # type: ignore[method-assign]
    client.forward_audit = SimpleNamespace(
        _settle=fake_settle, reconcile_stale_settlements=fake_reconcile,
    )
    client.autonomous = SimpleNamespace()
    client.stream_shadow = SimpleNamespace()
    client._boot_at = datetime.now(UTC) - timedelta(hours=1)
    client._log_acceptance_once = fake_acceptance  # type: ignore[method-assign]

    async def already_ready() -> None:
        return None

    client.wait_until_ready = already_ready  # type: ignore[method-assign]
    client.is_closed = lambda: False  # type: ignore[method-assign]

    health = asyncio.create_task(client._health_loop())
    try:
        await asyncio.wait_for(publish_seen.wait(), timeout=10)
        await asyncio.wait_for(acceptance_seen.wait(), timeout=5)
    finally:
        await _stop(health)
        await client.zip_resolver.aclose()

    assert seen["inspect"] == [WATCHDOG_STATEMENT_TIMEOUT_MS]
    assert seen["settle"] == [WATCHDOG_STATEMENT_TIMEOUT_MS]
    assert seen["reconcile"] == [WATCHDOG_STATEMENT_TIMEOUT_MS]
    assert seen["publish"] == [WATCHDOG_STATEMENT_TIMEOUT_MS]
    # The acceptance join is spawned AFTER the scoped blocks restore the
    # ambient (unlimited) bound, so its ~20 s first read is not killed.
    assert seen["acceptance"] == [None]
