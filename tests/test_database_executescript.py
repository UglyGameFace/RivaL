"""PostgreSQL executescript must not split statements inside SQL comments."""
from __future__ import annotations

from parlay_bot.storage.database import _Connection


class _RecordingRaw:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)


def test_semicolon_in_comment_does_not_create_a_bogus_statement() -> None:
    raw = _RecordingRaw()
    _Connection(raw, "postgres").executescript(
        """
        CREATE TABLE IF NOT EXISTS t (a INTEGER);
        -- returned rows; INCLUDE is not used because this DDL
        -- must also run on SQLite.
        CREATE INDEX IF NOT EXISTS idx_t_a ON t (a);
        """
    )
    assert raw.statements[0] == "SELECT pg_advisory_xact_lock(72649113001)"
    assert raw.statements[1:] == [
        "CREATE TABLE IF NOT EXISTS t (a INTEGER)",
        "CREATE INDEX IF NOT EXISTS idx_t_a ON t (a)",
    ]

