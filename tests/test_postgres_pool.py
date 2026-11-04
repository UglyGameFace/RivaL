"""PostgresDatabase connection pool: reuse, exhaustion, hygiene, close.

The pool is stdlib-only (queue + lock) and Postgres-only; SQLite is
untouched. psycopg.connect is faked so no server is needed.
"""

from __future__ import annotations

import threading
from typing import Self

import pytest
from psycopg.pq import TransactionStatus

from parlay_bot.storage.database import PostgresDatabase


class _FakeInfo:
    def __init__(self) -> None:
        self.transaction_status = TransactionStatus.IDLE


class _FakeConnection:
    def __init__(self) -> None:
        self.info = _FakeInfo()
        self._closed = False
        self.rollback_calls = 0
        self.close_calls = 0
        self.rollback_raises = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = ()) -> object:
        return None

    def rollback(self) -> None:
        if self.rollback_raises:
            raise RuntimeError("connection broken")
        self.rollback_calls += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self) -> None:
        self._closed = True
        self.close_calls += 1


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> dict:
    created: list[_FakeConnection] = []

    def _connect(*args: object, **kwargs: object) -> _FakeConnection:
        conn = _FakeConnection()
        created.append(conn)
        return conn

    monkeypatch.setattr("psycopg.connect", _connect)
    return {"created": created}


def test_sequential_checkouts_reuse_one_connection(
    fake_connect: dict,
) -> None:
    db = PostgresDatabase("postgresql://user:pass@host/db")
    with db.connect() as first:
        raw_first = first._raw
    with db.connect() as second:
        raw_second = second._raw
    assert raw_first is raw_second
    assert len(fake_connect["created"]) == 1


def test_pool_exhaustion_times_out_and_recovers(
    fake_connect: dict,
) -> None:
    db = PostgresDatabase(
        "postgresql://user:pass@host/db", max_size=1, checkout_timeout=0.3,
    )
    started = threading.Event()
    error: list[BaseException | None] = []
    done = threading.Event()

    def _worker() -> None:
        started.set()
        try:
            with db.connect():
                pass
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            error.append(exc)
        done.set()

    with db.connect():
        worker = threading.Thread(target=_worker)
        worker.start()
        assert started.wait(timeout=5)
        # Main holds the only connection longer than the worker timeout,
        # so the worker's attempt must time out deterministically.
        assert done.wait(timeout=5)
        assert len(error) == 1
        assert isinstance(error[0], TimeoutError)
    worker.join(timeout=5)
    # After release, a fresh checkout succeeds and still reuses the pool.
    with db.connect():
        pass
    assert len(fake_connect["created"]) == 1


def test_checkin_rolls_back_open_transaction(fake_connect: dict) -> None:
    db = PostgresDatabase("postgresql://user:pass@host/db")
    with db.connect() as conn:
        raw = conn._raw
        raw.info.transaction_status = TransactionStatus.INTRANS
    assert raw.rollback_calls == 1
    assert raw.info.transaction_status == TransactionStatus.IDLE
    with db.connect() as conn:
        assert conn._raw is raw
    assert len(fake_connect["created"]) == 1


def test_broken_connection_is_discarded(fake_connect: dict) -> None:
    db = PostgresDatabase("postgresql://user:pass@host/db")
    with db.connect() as conn:
        raw = conn._raw
        raw.info.transaction_status = TransactionStatus.INTRANS
        raw.rollback_raises = True
    assert raw.close_calls == 1
    with db.connect() as conn:
        assert conn._raw is not raw
    assert len(fake_connect["created"]) == 2


def test_close_drains_idle_and_rejects_new_checkouts(
    fake_connect: dict,
) -> None:
    db = PostgresDatabase("postgresql://user:pass@host/db")
    with db.connect() as conn:
        raw = conn._raw
    db.close()
    assert raw.close_calls == 1
    db.close()  # idempotent
    with pytest.raises(RuntimeError, match="pool is closed"):
        db.connect()


def test_checked_out_connection_closes_on_checkin_after_close(
    fake_connect: dict,
) -> None:
    db = PostgresDatabase("postgresql://user:pass@host/db")
    with db.connect() as conn:
        raw = conn._raw
        db.close()
    assert raw.close_calls == 1


def test_read_only_pool_marks_each_checkout_and_blocks_schema_advisory_lock(
    fake_connect: dict,
) -> None:
    db = PostgresDatabase("postgresql://user:pass@host/db", read_only=True)
    with db.connect() as conn:
        assert conn._read_only is True
        with pytest.raises(PermissionError, match="Read-only"):
            conn.executescript("CREATE TABLE bad (n INTEGER)")
    with db.connect() as conn:
        assert conn._read_only is True
    assert len(fake_connect["created"]) == 1
    db.close()


def test_read_only_postgres_rejects_actual_writes_when_service_present() -> None:
    import os

    import psycopg

    from parlay_bot.storage.hot import PostgresHotStore

    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration service unavailable")
    writer = PostgresHotStore(dsn)
    reader = PostgresHotStore(dsn, read_only=True)
    try:
        with writer.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rival_read_only_probe_test "
                "(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO rival_read_only_probe_test(id,value) VALUES (1,10) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value"
            )
        with reader.connect() as conn:
            assert conn.execute(
                "SELECT value FROM rival_read_only_probe_test WHERE id=1"
            ).fetchone()["value"] == 10
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("UPDATE rival_read_only_probe_test SET value=99 WHERE id=1")
        with reader.connect() as conn, pytest.raises(PermissionError, match="Read-only"):
            conn.executescript("CREATE TABLE rival_should_never_exist (id INTEGER)")
        with writer.connect() as conn:
            assert conn.execute(
                "SELECT value FROM rival_read_only_probe_test WHERE id=1"
            ).fetchone()["value"] == 10
    finally:
        with writer.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS rival_read_only_probe_test")
        reader.close()
        writer.close()
