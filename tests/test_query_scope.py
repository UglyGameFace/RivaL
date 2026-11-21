"""Background statement-timeout scope: worker default + integration proofs.

Unit tests pin run_background's semantics. The PostgreSQL tests (skipped
without RIVAL_TEST_DATABASE_URL) prove the two production claims of the
pool-safety change: a warm hot-store call costs ~1 statement instead of
~24, and a statement parked on a lock under the background bound aborts
with QueryCanceled and returns its pooled connection.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pytest

from parlay_bot.storage.query_scope import (
    BACKGROUND_STATEMENT_TIMEOUT_MS,
    current_statement_timeout_ms,
    run_background,
    statement_timeout,
)


def test_run_background_sets_the_default_for_the_worker() -> None:
    seen: dict[str, int | None] = {}

    async def worker() -> None:
        seen["inside"] = current_statement_timeout_ms()
        with statement_timeout(12_000):
            seen["nested"] = current_statement_timeout_ms()
        seen["restored"] = current_statement_timeout_ms()

    asyncio.run(run_background(worker()))
    assert seen == {
        "inside": BACKGROUND_STATEMENT_TIMEOUT_MS,
        "nested": 12_000,
        "restored": BACKGROUND_STATEMENT_TIMEOUT_MS,
    }
    # The bound lives in the worker task's context; it must not leak out.
    assert current_statement_timeout_ms() is None


def _scoped_hot_store() -> tuple[object, str, str]:
    """A PostgresHotStore confined to a throwaway schema (conftest pattern)."""
    import psycopg
    from psycopg import sql

    from parlay_bot.storage.hot import PostgresHotStore

    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL CI service required")
    schema = "scope_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query))
    query["options"] = (query.get("options", "") + f" -c search_path={schema}").strip()
    scoped_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    return PostgresHotStore(scoped_url), scoped_url, schema


def _drop_schema(scoped_url: str, schema: str) -> None:
    import psycopg
    from psycopg import sql

    parts = urlsplit(scoped_url)
    base = urlunsplit(parts._replace(query=""))
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_hot_store_warm_call_costs_one_statement_postgres() -> None:
    from parlay_bot.storage.database import database_clock

    store, scoped_url, schema = _scoped_hot_store()
    try:
        store.set_runtime_state("probe", "1")  # first call pays schema DDL
        with database_clock() as clock:
            assert store.get_runtime_state("probe") == "1"
        # Before the initialize() guard: 24 statements across 2 checkouts.
        assert clock.checkouts == 1
        assert clock.statements <= 2
    finally:
        store.close()
        _drop_schema(scoped_url, schema)


def test_lock_parked_statement_aborts_and_returns_its_connection() -> None:
    import psycopg
    from psycopg.errors import QueryCanceled

    from parlay_bot.storage.diagnostics import QueryDiagnostics
    from parlay_bot.storage.query_scope import mark_background

    store, scoped_url, schema = _scoped_hot_store()
    database = store.database
    try:
        store.set_runtime_state("probe", "1")
        locker = psycopg.connect(scoped_url)
        try:
            locker.execute("LOCK TABLE runtime_state IN ACCESS EXCLUSIVE MODE")
            outcome: dict[str, object] = {}

            def worker() -> None:
                # A previously-unbounded loop's code path, now under the
                # worker default (300 ms here so the test stays fast).
                mark_background(300)
                started = time.monotonic()
                try:
                    store.get_runtime_state("probe")
                except Exception as exc:  # noqa: BLE001 - asserted below
                    outcome["error"] = exc
                outcome["elapsed"] = time.monotonic() - started

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert isinstance(outcome.get("error"), QueryCanceled)
            assert float(outcome["elapsed"]) < 5.0
            snapshot = QueryDiagnostics().collect(SimpleNamespace(database=database))
            assert snapshot["pool"]["checked_out"] == 0
        finally:
            locker.rollback()
            locker.close()
    finally:
        store.close()
        _drop_schema(scoped_url, schema)
