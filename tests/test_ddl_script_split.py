"""One DDL splitter for both backends, so tests catch what would break deploy."""
from __future__ import annotations

import pytest

from parlay_bot.storage.database import split_script


def test_semicolons_in_comments_never_split_a_statement():
    script = """
        -- INCLUDE is not used; this once crash-looped startup
        CREATE TABLE a (x INTEGER); /* block; comment */
        CREATE INDEX a_x ON a (x) -- trailing; comment
    """
    assert split_script(script) == [
        "CREATE TABLE a (x INTEGER)", "CREATE INDEX a_x ON a (x)"]


def test_quoted_semicolons_and_dashes_are_kept():
    script = "INSERT INTO t VALUES ('a;b', 'it''s -- not a comment'); SELECT \"c;d\""
    assert split_script(script) == [
        "INSERT INTO t VALUES ('a;b', 'it''s -- not a comment')", 'SELECT "c;d"']


def test_malformed_scripts_fail_loudly():
    with pytest.raises(ValueError):
        split_script("SELECT 'open")
    with pytest.raises(ValueError):
        split_script("SELECT 1 /* open")


def test_sqlite_runs_the_same_split(tmp_path):
    from parlay_bot.storage.database import SQLiteDatabase

    store = SQLiteDatabase(tmp_path / "x.db")
    with store.connect() as conn:
        conn.executescript("""
            CREATE TABLE t (x INTEGER); -- note; with a semicolon
            INSERT INTO t VALUES (1);
        """)
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
    # A statement the old whole-script path would have hidden still fails here.
    with store.connect() as conn, pytest.raises(Exception):
        conn.executescript("CREATE TABLE u (x INTEGER); NOT SQL AT ALL")
