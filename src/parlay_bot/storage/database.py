from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, Self

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

_LOG = logging.getLogger(__name__)

BackendName = Literal["sqlite", "postgres"]


class CursorLike(Protocol):
    rowcount: int

    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


class ConnectionLike(Protocol):
    def execute(self, sql: str, params: Sequence[Any] = ()) -> CursorLike: ...
    def executescript(self, script: str) -> None: ...


def _postgres_sql(sql: str) -> str:
    """Translate the project's qmark placeholders to psycopg placeholders."""
    return sql.replace("?", "%s")


class _Connection:
    def __init__(
        self, raw: Any, backend: BackendName,
        on_exit: Callable[[Any], None] | None = None,
    ) -> None:
        self._raw = raw
        self.backend = backend
        self._on_exit = on_exit

    def execute(self, sql: str, params: Sequence[Any] = ()) -> CursorLike:
        if self.backend == "postgres":
            sql = _postgres_sql(sql)
        return self._raw.execute(sql, tuple(params))

    def executescript(self, script: str) -> None:
        if self.backend == "sqlite":
            self._raw.executescript(script)
            return

        # IF NOT EXISTS is not concurrency-safe when two independent workers
        # create the same PostgreSQL catalog entry at the same instant.
        # A transaction-scoped advisory lock serializes startup DDL across
        # threads, store instances, and even overlapping bot processes.
        # It is released automatically at commit/rollback; no permanent lock.
        self._raw.execute("SELECT pg_advisory_xact_lock(72649113001)")
        statements = [part.strip() for part in script.split(";") if part.strip()]
        for statement in statements:
            self._raw.execute(statement)

    def __enter__(self) -> Self:
        self._raw.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        try:
            if self.backend == "postgres" and hasattr(self._raw, "commit"):
                if not getattr(self._raw, "closed", False):
                    if exc_type is not None:
                        try:
                            self._raw.rollback()
                        except Exception:  # noqa: BLE001
                            _LOG.warning("RivaL PostgreSQL rollback failed during exception cleanup")
                    else:
                        self._raw.commit()
                result = False
            else:
                result = self._raw.__exit__(exc_type, exc, tb)
        finally:
            if self.backend == "sqlite":
                self._raw.close()
            elif self._on_exit is not None:
                self._on_exit(self._raw)
        return result


class RelationalDatabase:
    backend: BackendName

    def connect(self) -> _Connection:
        raise NotImplementedError

    def column_names(self, connection: ConnectionLike, table: str) -> set[str]:
        if self.backend == "sqlite":
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            return {str(row["name"]) for row in rows}

        rows = connection.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ?
            """,
            (table,),
        ).fetchall()
        return {str(row["name"]) for row in rows}

    @property
    def auto_increment_primary_key(self) -> str:
        if self.backend == "postgres":
            return "BIGSERIAL PRIMARY KEY"
        return "INTEGER PRIMARY KEY AUTOINCREMENT"

    def close(self) -> None:
        return None


class SQLiteDatabase(RelationalDatabase):
    backend: BackendName = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> _Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA foreign_keys=ON")
        return _Connection(raw, self.backend)


class PostgresDatabase(RelationalDatabase):
    backend: BackendName = "postgres"

    def __init__(
        self, dsn: str, *, max_size: int = 8, checkout_timeout: float = 10.0,
    ) -> None:
        cleaned = dsn.strip()
        if not cleaned:
            raise ValueError("PostgreSQL DSN is required")
        if not cleaned.startswith(("postgresql://", "postgres://")):
            raise ValueError("RIVAL_DATABASE_URL must be a PostgreSQL connection URL")
        if max_size < 1:
            raise ValueError("PostgreSQL pool max_size must be at least 1")
        if checkout_timeout <= 0:
            raise ValueError("PostgreSQL pool checkout_timeout must be positive")
        self.dsn = cleaned
        self._max_size = max_size
        self._checkout_timeout = checkout_timeout
        # Small stdlib pool, no new dependency: every render used to pay
        # a full TCP+TLS+auth handshake per query (10-15 fresh connections
        # per menu render). Connections here are reused across threads;
        # each checkout is used by one thread at a time via `with`.
        self._idle: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._total = 0
        self._closed = False
        # Diagnostic only: track checkout ages, never SQL/credentials.
        self._borrowers: dict[int, tuple[float, str]] = {}

    def connect(self) -> _Connection:
        started = time.monotonic()
        raw = self._checkout()
        held_since = time.monotonic()
        with self._lock:
            self._borrowers[id(raw)] = (
                held_since, threading.current_thread().name,
            )
            checked_out = len(self._borrowers)
        waited = held_since - started
        if waited >= 1.0:
            _LOG.warning(
                "RivaL PostgreSQL checkout slow wait=%.2fs checked_out=%d max=%d",
                waited, checked_out, self._max_size,
            )

        def release(connection: Any) -> None:
            with self._lock:
                self._borrowers.pop(id(connection), None)
            try:
                self._checkin(connection)
            finally:
                held = time.monotonic() - held_since
                if held >= 5.0:
                    _LOG.warning(
                        "RivaL PostgreSQL connection held %.2fs worker=%s",
                        held, threading.current_thread().name,
                    )

        return _Connection(raw, self.backend, on_exit=release)

    def _checkout(self) -> Any:
        while True:
            try:
                raw = self._idle.get_nowait()
            except queue.Empty:
                pass
            else:
                if raw.closed:
                    with self._lock:
                        self._total -= 1
                    continue
                return raw
            with self._lock:
                if self._closed:
                    raise RuntimeError(
                        "PostgreSQL connection pool is closed",
                    )
                create = self._total < self._max_size
                if create:
                    self._total += 1
            if create:
                try:
                    return psycopg.connect(
                        self.dsn,
                        row_factory=dict_row,
                        connect_timeout=10,
                    )
                except Exception:
                    with self._lock:
                        self._total -= 1
                    raise
            try:
                raw = self._idle.get(timeout=self._checkout_timeout)
            except queue.Empty:
                with self._lock:
                    ages = [
                        (time.monotonic() - borrowed, worker)
                        for borrowed, worker in self._borrowers.values()
                    ]
                oldest_age, oldest_worker = max(
                    ages, default=(0.0, "unknown"),
                )
                _LOG.error(
                    "RivaL PostgreSQL pool exhausted max=%d checked_out=%d "
                    "oldest_held=%.1fs oldest_worker=%s",
                    self._max_size, len(ages), oldest_age, oldest_worker,
                )
                raise TimeoutError(
                    "PostgreSQL connection pool exhausted "
                    f"(max_size={self._max_size}); all connections are "
                    "checked out",
                ) from None
            if raw.closed:
                with self._lock:
                    self._total -= 1
                continue
            return raw

    def _checkin(self, raw: Any) -> None:
        with self._lock:
            closed = self._closed
        if closed:
            try:
                raw.close()
            finally:
                with self._lock:
                    self._total -= 1
            return
        # A borrower may have left an open transaction (or a broken
        # connection) behind. Roll back so the next borrower sees clean
        # state; a connection that cannot roll back is discarded, never
        # returned to the pool.
        try:
            if raw.info.transaction_status != TransactionStatus.IDLE:
                raw.rollback()
        except Exception:  # noqa: BLE001 - a failed checkin must discard, not raise
            try:
                raw.close()
            finally:
                with self._lock:
                    self._total -= 1
            return
        if raw.closed:
            with self._lock:
                self._total -= 1
            return
        try:
            self._idle.put_nowait(raw)
        except Exception:  # noqa: BLE001 - a failed checkin must discard, not raise
            try:
                raw.close()
            finally:
                with self._lock:
                    self._total -= 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        while True:
            try:
                raw = self._idle.get_nowait()
            except queue.Empty:
                return
            try:
                raw.close()
            finally:
                with self._lock:
                    self._total -= 1
