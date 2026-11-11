"""An optional read fails alone: own connection, None, quiet for a missing table."""
from __future__ import annotations

import logging

from parlay_bot.storage.hot import SQLiteHotStore
from parlay_bot.storage.optional_reads import is_missing_table, optional_rows


def test_missing_table_is_none_and_quiet(tmp_path, caplog):
    store = SQLiteHotStore(tmp_path / "o.sqlite")
    try:
        with caplog.at_level(logging.WARNING):
            assert optional_rows(store, "SELECT * FROM not_there") is None
        assert not caplog.records
        with store.connect() as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
        assert [r["x"] for r in optional_rows(store, "SELECT x FROM t")] == [1]
    finally:
        store.close()


def test_other_failures_are_logged_and_none(caplog):
    class Broken:
        def connect(self):
            raise TimeoutError("statement timeout")

    with caplog.at_level(logging.WARNING):
        assert optional_rows(Broken(), "SELECT 1", what="test read") is None
    assert "test read failed (TimeoutError)" in caplog.text
    assert "SELECT" not in caplog.text            # SQL never enters logs


def test_missing_table_markers():
    assert is_missing_table(Exception('relation "x" does not exist'))
    assert is_missing_table(Exception("no such table: x"))
    assert not is_missing_table(TimeoutError("canceling statement due to timeout"))
