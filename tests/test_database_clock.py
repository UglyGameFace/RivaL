"""PostgreSQL time per thread, and which statement a slow warning names."""
from __future__ import annotations

import logging
import threading

from parlay_bot.storage import database
from parlay_bot.storage.database import _Connection, _statement_label, database_clock


class _FakeRaw:
    """Stands in for a psycopg connection; each execute 'takes' a fixed time."""

    def __init__(self, clock_values):
        self._clock = clock_values
        self.closed = False

    def execute(self, sql, params=()):
        return None

    def commit(self):
        return None

    def __enter__(self):
        return self


def _connection(monkeypatch, step):
    ticks = iter(range(10_000))
    monkeypatch.setattr(database.time, "monotonic", lambda: next(ticks) * step)
    return _Connection(_FakeRaw(None), "postgres")


def test_clock_counts_sql_and_commit_only_on_its_own_thread(monkeypatch):
    conn = _connection(monkeypatch, 0.5)
    with database_clock() as clock:
        conn.execute("SELECT 1 FROM perp_forward_audit")
        conn.execute("UPDATE rival_model_shadow_forecasts SET settled_at=?", ("x",))
        conn.__exit__(None, None, None)
        seen = []
        other = threading.Thread(target=lambda: seen.append(database._active_clock()))
        other.start()
        other.join()
    assert clock.statements == 2 and clock.commits == 1
    assert clock.sql_seconds == 1.0 and clock.commit_seconds == 0.5
    assert seen == [None]  # another worker's queries are never counted here
    assert database._active_clock() is None


def test_clocks_nest_and_restore(monkeypatch):
    conn = _connection(monkeypatch, 0.1)
    with database_clock() as outer:
        with database_clock() as inner:
            conn.execute("SELECT 1")
        conn.execute("SELECT 2")
    assert inner.statements == 1 and outer.statements == 1


def test_slow_warning_names_the_table_and_statement_not_values(monkeypatch, caplog):
    conn = _connection(monkeypatch, 3.0)
    secret = "SELECT * FROM rival_problem_log WHERE text='token-abc'"
    with caplog.at_level(logging.WARNING, logger=database.__name__):
        conn.execute(secret)
    message = caplog.records[-1].getMessage()
    assert "statement=rival_problem_log#" in message
    assert "token-abc" not in message and "text=" not in message
    assert _statement_label("SELECT 1") == f"none#{_statement_label('SELECT 1')[-8:]}"
    assert _statement_label(secret) != _statement_label(secret + " LIMIT 1")
