"""Discord interaction work gets a PostgreSQL statement limit; workers don't."""
from __future__ import annotations

import asyncio
import contextvars
import os

import psycopg
import pytest
from discord import app_commands
from discord.ui.modal import Modal
from discord.ui.view import BaseView

from parlay_bot.discord_app import interaction_scope
from parlay_bot.storage import query_scope
from parlay_bot.storage.database import PostgresDatabase


def test_unmarked_context_has_no_limit():
    assert contextvars.Context().run(query_scope.current_statement_timeout_ms) is None


def test_mark_rejects_unreasonable_limits():
    for bad in (0, 99, 600_001):
        with pytest.raises(ValueError):
            contextvars.Context().run(query_scope.mark_interactive, bad)


@pytest.mark.asyncio
async def test_limit_follows_task_and_threads_but_not_other_tasks():
    async def interaction():
        query_scope.mark_interactive()
        return await asyncio.to_thread(query_scope.current_statement_timeout_ms)

    async def worker():
        await asyncio.sleep(0)
        return await asyncio.to_thread(query_scope.current_statement_timeout_ms)

    tapped, background = await asyncio.gather(
        asyncio.create_task(interaction(), context=contextvars.Context()),
        asyncio.create_task(worker(), context=contextvars.Context()),
    )
    assert tapped == query_scope.INTERACTIVE_STATEMENT_TIMEOUT_MS
    assert background is None


def test_discord_private_dispatch_hooks_still_exist():
    # If a discord.py upgrade renames these, fail here rather than silently
    # running view taps without the interactive limit.
    assert callable(BaseView._scheduled_task)
    assert callable(Modal._scheduled_task)


@pytest.mark.asyncio
async def test_install_marks_tree_checks_and_component_dispatch():
    tree = app_commands.CommandTree.__new__(app_commands.CommandTree)
    interaction_scope.install_interaction_scope(tree)
    interaction_scope.install_interaction_scope(tree)  # idempotent
    assert getattr(BaseView._scheduled_task, interaction_scope._INSTALLED)
    assert getattr(Modal._scheduled_task, interaction_scope._INSTALLED)

    async def run_check():
        allowed = await tree.interaction_check(object())
        return allowed, query_scope.current_statement_timeout_ms()

    allowed, limit = await asyncio.create_task(
        run_check(), context=contextvars.Context(),
    )
    assert allowed is True
    assert limit == query_scope.INTERACTIVE_STATEMENT_TIMEOUT_MS


def _pg() -> PostgresDatabase:
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL CI service")
    return PostgresDatabase(dsn, max_size=1)


def _show(db: PostgresDatabase) -> str:
    with db.connect() as conn:
        return str(conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"])


def test_postgres_limit_applies_only_inside_marked_scope_and_resets():
    db = _pg()
    try:
        assert _show(db) == "0"

        def interactive():
            query_scope.mark_interactive()
            return _show(db)

        assert contextvars.Context().run(interactive) == "8s"
        # Same pooled connection (max_size=1) is unlimited again for workers.
        assert _show(db) == "0"
    finally:
        db.close()


def test_postgres_interactive_query_is_cancelled_after_limit():
    db = _pg()
    try:
        def slow():
            query_scope.mark_interactive(100)
            with db.connect() as conn:
                conn.execute("SELECT pg_sleep(1)")

        with pytest.raises(psycopg.errors.QueryCanceled):
            contextvars.Context().run(slow)
        # The connection survives and serves an unlimited worker query.
        with db.connect() as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        db.close()
