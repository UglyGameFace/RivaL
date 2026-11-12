from __future__ import annotations

import hashlib
import logging
import queue
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, Self

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from parlay_bot.storage.query_scope import current_statement_timeout_ms

_LOG = logging.getLogger(__name__)

BackendName = Literal["sqlite", "postgres"]


class CursorLike(Protocol):
    rowcount: int

    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


class ConnectionLike(Protocol):
    def execute(self, sql: str, params: Sequence[Any] = ()) -> CursorLike: ...
    def executescript(self, script: str) -> None: ...


class DatabaseClock:
    """PostgreSQL time spent by ONE thread while a clock is active.

    Lets a caller split its own wall time into SQL (execute, including lock
    waits and result transfer), commit, and pool-checkout wait, versus the
    Python work in between. Names and totals only, never SQL or values.
    """

    __slots__ = ("checkouts", "commit_seconds", "commits", "sql_seconds",
                 "statements", "wait_seconds")

    def __init__(self) -> None:
        self.sql_seconds = self.commit_seconds = self.wait_seconds = 0.0
        self.statements = self.commits = self.checkouts = 0

    @property
    def seconds(self) -> float:
        return self.sql_seconds + self.commit_seconds + self.wait_seconds


_CLOCKS = threading.local()


class database_clock:
    """``with database_clock() as clock:`` measures this thread's PostgreSQL time."""

    def __enter__(self) -> DatabaseClock:
        self._previous = getattr(_CLOCKS, "current", None)
        self._clock = DatabaseClock()
        _CLOCKS.current = self._clock
        return self._clock

    def __exit__(self, *exc: object) -> None:
        _CLOCKS.current = self._previous


def _active_clock() -> DatabaseClock | None:
    return getattr(_CLOCKS, "current", None)


_FIRST_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _statement_label(sql: str) -> str:
    """Which statement was slow, without SQL text or values: first table + digest.

    One caller often runs several SELECTs on one connection; the operation
    word alone ("SELECT") could not say which one. The 8-hex digest of the
    statement text tells statements on the same table apart.
    """
    match = _FIRST_TABLE.search(sql)
    table = match.group(1).lower() if match else "none"
    return f"{table}#{hashlib.sha256(sql.encode()).hexdigest()[:8]}"


def _owner_group(origin: str) -> str:
    """Problem-history group for a caller: module.function, no line number."""
    return str(origin).split(":", 1)[0]


def _postgres_sql(sql: str) -> str:
    """Translate the project's qmark placeholders to psycopg placeholders."""
    # Psycopg treats every percent sign as placeholder syntax when parameters
    # are supplied. SQL LIKE patterns (e.g. qualified|%) must escape literal
    # percent signs *before* qmark parameters are translated to %s.
    return sql.replace("%", "%%").replace("?", "%s")


def split_script(script: str) -> list[str]:
    """Split a DDL script into statements, the same way on both backends.

    ``--`` comments are dropped and ``;`` ends a statement only outside a
    quoted string or identifier. A semicolon inside a comment once split a
    PostgreSQL statement mid-comment and crash-looped startup, while the
    SQLite tests (whole-script executescript) passed.
    """
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if quote is not None:
            current.append(ch)
            if ch == quote:
                if i + 1 < n and script[i + 1] == quote:  # escaped '' or ""
                    current.append(script[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == "-" and script.startswith("--", i):
            end = script.find("\n", i)
            i = n if end < 0 else end
            continue
        elif ch == "/" and script.startswith("/*", i):
            end = script.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated /* comment in DDL script")
            i = end + 2
            current.append(" ")
            continue
        elif ch == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(ch)
        i += 1
    if quote is not None:
        raise ValueError("unterminated quote in DDL script")
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class _Connection:
    def __init__(
        self, raw: Any, backend: BackendName,
        on_exit: Callable[[Any], None] | None = None,
        origin: str = "unknown", read_only: bool = False,
        diagnostics=None,
    ) -> None:
        self._read_only = read_only
        self._raw = raw
        self.backend = backend
        self._on_exit = on_exit
        # Only operation names and timings, never SQL text, parameters,
        # URLs, API tokens or database values, enter diagnostics.
        self.origin = origin
        self.diagnostics = diagnostics
        self.query_count = 0
        self.query_seconds = 0.0
        self.max_query_seconds = 0.0
        self.slowest_operation = "none"
        self.active_operation = "none"
        self.active_started = 0.0
        self.commit_seconds = 0.0

    def execute(self, sql: str, params: Sequence[Any] = ()) -> CursorLike:
        if self.backend != "postgres":
            return self._raw.execute(sql, tuple(params))
        operation = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else "UNKNOWN"
        # The live owner may be monitored by another thread at pool timeout.
        started = time.monotonic()
        self.active_operation = operation
        self.active_started = started
        error = None
        try:
            return self._raw.execute(_postgres_sql(sql), tuple(params))
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            elapsed = time.monotonic() - started
            if self.diagnostics is not None and (elapsed >= 2 or error is not None):
                self.diagnostics.observe(owner=self.origin, label=_statement_label(sql),
                                         sql=sql, params=params, seconds=elapsed, error=error)
            self.query_count += 1
            self.query_seconds += elapsed
            clock = _active_clock()
            if clock is not None:
                clock.sql_seconds += elapsed
                clock.statements += 1
            if self.query_count == 1 or elapsed > self.max_query_seconds:
                self.max_query_seconds = elapsed
                self.slowest_operation = operation
            self.active_operation = "none"
            self.active_started = 0.0
            if elapsed >= 2.0:
                _LOG.warning(
                    "RivaL PostgreSQL slow operation owner=%s operation=%s "
                    "statement=%s duration=%.2fs (SQL/parameters omitted)",
                    self.origin, operation, _statement_label(sql), elapsed,
                    extra={"problem_group": _owner_group(self.origin)},
                )

    def executescript(self, script: str) -> None:
        if self._read_only:
            # Do not even acquire the shared DDL advisory lock when a
            # diagnostic script accidentally requests schema initialization.
            raise PermissionError("Read-only PostgreSQL connection cannot initialize schema")
        if self.backend == "sqlite":
            # Same split as PostgreSQL, so a script that only SQLite's
            # executescript would accept fails in tests, not at deploy.
            # executescript committed any open transaction first; keep that.
            if self._raw.in_transaction:
                self._raw.commit()
            for statement in split_script(script):
                self._raw.execute(statement)
            return

        # IF NOT EXISTS is not concurrency-safe when two independent workers
        # create the same PostgreSQL catalog entry at the same instant.
        # A transaction-scoped advisory lock serializes startup DDL across
        # threads, store instances, and even overlapping bot processes.
        # It is released automatically at commit/rollback; no permanent lock.
        self.execute("SELECT pg_advisory_xact_lock(72649113001)")
        for statement in split_script(script):
            self.execute(statement)

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
                        started = time.monotonic()
                        try:
                            self._raw.commit()
                        finally:
                            spent = time.monotonic() - started
                            self.commit_seconds += spent
                            clock = _active_clock()
                            if clock is not None:
                                clock.commit_seconds += spent
                                clock.commits += 1
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
        read_only: bool = False,
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
        self._read_only = read_only
        # Small stdlib pool, no new dependency: every render used to pay
        # a full TCP+TLS+auth handshake per query (10-15 fresh connections
        # per menu render). Connections here are reused across threads;
        # each checkout is used by one thread at a time via `with`.
        self._idle: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._total = 0
        self._closed = False
        # Diagnostic only: track checkout ages, never SQL/credentials.
        self._borrowers: dict[int, tuple[float, str, _Connection]] = {}
        from parlay_bot.storage.diagnostics import QueryDiagnostics

        self.diagnostics = QueryDiagnostics()

    @staticmethod
    def _checkout_origin() -> str:
        """Find the first application caller above storage without stack payloads."""
        frame = sys._getframe(1)
        for _ in range(16):
            if frame is None:
                break
            module = str(frame.f_globals.get("__name__", ""))
            if (module.startswith("parlay_bot.")
                    and module not in (
                        "parlay_bot.storage.database",
                        "parlay_bot.storage.hot",
                    )):
                return (f"{module.rsplit('.', 1)[-1]}."
                        f"{frame.f_code.co_name}:{frame.f_lineno}")
            frame = frame.f_back
        return "unattributed"

    def connect(self) -> _Connection:
        started = time.monotonic()
        raw = self._checkout()
        limit_ms = current_statement_timeout_ms()
        try:
            if self._read_only:
                # PostgreSQL, not a Python SQL-string heuristic, enforces
                # SELECT-only behavior on every pooled checkout. Each
                # transaction loses this setting automatically at commit.
                raw.execute("SET TRANSACTION READ ONLY")
            if limit_ms is not None:
                # SET LOCAL lasts only for this borrower's transaction.
                raw.execute(f"SET LOCAL statement_timeout = {int(limit_ms)}")
        except Exception:
            self._checkin(raw)
            raise
        held_since = time.monotonic()
        owner = self._checkout_origin()
        borrowed = _Connection(raw, self.backend, origin=owner,
                               read_only=self._read_only, diagnostics=self.diagnostics)
        with self._lock:
            self._borrowers[id(raw)] = (
                held_since, threading.current_thread().name, borrowed,
            )
            checked_out = len(self._borrowers)
        waited = held_since - started
        clock = _active_clock()
        if clock is not None:
            clock.wait_seconds += waited
            clock.checkouts += 1
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
                        "RivaL PostgreSQL connection held %.2fs owner=%s "
                        "worker=%s queries=%d sql_total=%.2fs "
                        "slowest=%s:%.2fs commit_total=%.2fs "
                        "(SQL/parameters omitted)",
                        held, borrowed.origin, threading.current_thread().name,
                        borrowed.query_count, borrowed.query_seconds,
                        borrowed.slowest_operation, borrowed.max_query_seconds,
                        borrowed.commit_seconds,
                        extra={"problem_group": _owner_group(borrowed.origin)},
                    )

        borrowed._on_exit = release
        return borrowed

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
                        (time.monotonic() - started, worker, borrowed)
                        for started, worker, borrowed in self._borrowers.values()
                    ]
                oldest_age, oldest_worker, oldest_connection = max(
                    ages, key=lambda item: item[0],
                    default=(0.0, "unknown", None),
                )
                active = (
                    oldest_connection.active_operation
                    if oldest_connection is not None else "none"
                )
                active_seconds = (
                    max(0.0, time.monotonic() - oldest_connection.active_started)
                    if oldest_connection is not None
                    and oldest_connection.active_started else 0.0
                )
                _LOG.error(
                    "RivaL PostgreSQL pool exhausted max=%d checked_out=%d "
                    "oldest_held=%.1fs oldest_worker=%s owner=%s "
                    "active_operation=%s active_seconds=%.1fs",
                    self._max_size, len(ages), oldest_age, oldest_worker,
                    oldest_connection.origin if oldest_connection else "unknown",
                    active, active_seconds,
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
