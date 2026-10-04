from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, Self

import psycopg
from psycopg.rows import dict_row


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
    def __init__(self, raw: Any, backend: BackendName) -> None:
        self._raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Sequence[Any] = ()) -> CursorLike:
        if self.backend == "postgres":
            sql = _postgres_sql(sql)
        return self._raw.execute(sql, tuple(params))

    def executescript(self, script: str) -> None:
        if self.backend == "sqlite":
            self._raw.executescript(script)
            return

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
            result = self._raw.__exit__(exc_type, exc, tb)
        finally:
            if self.backend == "sqlite":
                self._raw.close()
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

    def __init__(self, dsn: str) -> None:
        cleaned = dsn.strip()
        if not cleaned:
            raise ValueError("PostgreSQL DSN is required")
        if not cleaned.startswith(("postgresql://", "postgres://")):
            raise ValueError("RIVAL_DATABASE_URL must be a PostgreSQL connection URL")
        self.dsn = cleaned

    def connect(self) -> _Connection:
        raw = psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            connect_timeout=10,
        )
        return _Connection(raw, self.backend)
