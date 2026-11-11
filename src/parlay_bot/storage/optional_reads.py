"""Reads that may fail without taking anything else down with them.

On PostgreSQL a failed statement (a table that does not exist yet, a
statement timeout) aborts the whole transaction: every later statement on
that connection fails with "current transaction is aborted" until it ends.
Code that did

    with store.connect() as conn:
        try: a = conn.execute(optional_sql).fetchall()
        except Exception: a = []
        b = conn.execute(other_sql).fetchall()   # fails, or is swallowed too

turned one missing table or one slow read into wrong "no data yet" screens
and database errors. ``optional_rows`` gives each optional read its own
short connection, so a failure stays with that read.

A missing table is normal on a fresh install (it appears after its first
writer runs) and is not logged; any other failure is logged once per call
as a warning, never raised: these reads only decorate a screen.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

_LOG = logging.getLogger(__name__)

_MISSING = ("no such table", "does not exist", "undefinedtable")


def is_missing_table(error: BaseException) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in _MISSING)


def optional_rows(store, sql: str, params: Sequence[Any] = (), *,
                  what: str = "optional read") -> list | None:
    """The rows, or None if the read failed (missing table: also None).

    None lets a screen say "couldn't read right now" instead of showing a
    fake zero; callers that treat "no table" and "no rows" alike use
    ``or []``.
    """
    try:
        with store.connect() as conn:
            return list(conn.execute(sql, tuple(params)).fetchall())
    except Exception as error:  # noqa: BLE001 - optional by definition
        if not is_missing_table(error):
            _LOG.warning("RivaL %s failed (%s); shown as unavailable",
                         what, type(error).__name__)
        return None
