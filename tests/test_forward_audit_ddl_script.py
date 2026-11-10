"""Postgres executescript split hazard in ForwardAccuracyMonitor DDL.

PR #485 crashed RivaL at startup: a "--" comment inside the DDL script
passed to conn.executescript() contained a semicolon ("returned rows;
INCLUDE is not used because this DDL"), and the Postgres path in
storage/database.py splits the script naively on ";" — so the split cut
the comment in half and produced a bare statement starting "INCLUDE is
not used because this DDL", which Postgres rejected with a SyntaxError.
SQLite runs executescript natively, so the sandbox suite could never
catch it; these tests replay the Postgres split on the real script
string and prove every piece is a real statement.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor

# First word of a statement the Postgres split may legitimately produce.
_SQL_KEYWORDS = frozenset({
    "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "SELECT",
    "WITH", "GRANT", "REVOKE", "TRUNCATE", "VACUUM", "ANALYZE", "COMMENT",
})


def _ddl_script() -> str:
    """The literal SQL string _initialize_schema passes to executescript."""
    source = inspect.getsource(ForwardAccuracyMonitor._initialize_schema)
    tree = ast.parse(textwrap.dedent(source))
    scripts = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "CREATE TABLE" in node.value
    ]
    assert scripts, "expected the DDL script literal in _initialize_schema"
    assert len(scripts) == 1, f"expected one DDL script, found {len(scripts)}"
    return scripts[0]


def _postgres_split(script: str) -> list[str]:
    """The exact split the Postgres executescript path performs."""
    return [part.strip() for part in script.split(";") if part.strip()]


def test_ddl_script_has_no_prose_comments():
    script = _ddl_script()
    assert "--" not in script, (
        "prose comments must not live inside the DDL script: the Postgres "
        "executescript path splits on ';' and a semicolon inside a comment "
        "breaks the split (PR #485 startup crash)"
    )


def test_ddl_script_splits_into_real_statements():
    parts = _postgres_split(_ddl_script())
    assert parts, "expected at least one DDL statement"
    for part in parts:
        first = part.split(None, 1)[0].upper()
        assert first in _SQL_KEYWORDS, (
            "Postgres split produced a non-statement piece starting "
            f"with {first!r}: {part[:80]!r}"
        )
