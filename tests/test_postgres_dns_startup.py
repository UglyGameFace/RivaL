"""PostgreSQL startup DNS recovery must never redirect or reset user data."""
from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from parlay_bot.storage.startup import (
    _is_dns_operational_error,
    initialize_with_dns_recovery,
)


def store(*, backend="postgres", effects=None):
    init = Mock(side_effect=effects)
    return SimpleNamespace(database=SimpleNamespace(backend=backend), initialize=init)


def test_temporary_dns_error_recovers_same_database_after_bounded_wait():
    attempts = [
        psycopg.OperationalError("failed to resolve host 'internal-db'"),
        psycopg.OperationalError("Name or service not known"),
        None,
    ]
    database = store(effects=attempts)
    pauses = []
    initialize_with_dns_recovery(database, sleep=pauses.append)
    assert database.initialize.call_count == 3
    assert pauses == [2.0, 4.0]


def test_unknown_permanent_host_fails_closed_with_no_password_leak(caplog):
    bad = psycopg.OperationalError(
        "failed to resolve host 'uglygameface138': [Errno -2] Name or service not known"
    )
    database = store(effects=[bad] * 3)
    waits = []
    with pytest.raises(RuntimeError, match="private hostname cannot resolve") as exc:
        initialize_with_dns_recovery(database, pauses=(0.1, 0.2), sleep=waits.append)
    assert database.initialize.call_count == 3
    assert waits == [0.1, 0.2]
    assert "uglygameface138" not in str(exc.value)
    assert "uglygameface138" not in caplog.text
    assert "Discloud" in str(exc.value)
    assert "VLAN" in caplog.text


def test_other_database_failures_not_retried_or_masked():
    bad = psycopg.OperationalError("password authentication failed for user dbuser")
    database = store(effects=[bad])
    waits = []
    with pytest.raises(psycopg.OperationalError, match="password authentication"):
        initialize_with_dns_recovery(database, sleep=waits.append)
    assert database.initialize.call_count == 1
    assert waits == []


def test_refused_connection_does_not_get_treated_as_dns():
    bad = psycopg.OperationalError("connection refused")
    database = store(effects=[bad])
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        initialize_with_dns_recovery(database, sleep=lambda _: pytest.fail("unexpected wait"))


def test_nested_socket_gaierror_is_retryable():
    try:
        raise socket.gaierror(-2, "name resolution failed")
    except socket.gaierror as exc:
        bad = psycopg.OperationalError("could not connect")
        bad.__cause__ = exc
    assert _is_dns_operational_error(bad)
    database = store(effects=[bad, None])
    waits = []
    initialize_with_dns_recovery(database, pauses=(1,), sleep=waits.append)
    assert database.initialize.call_count == 2
    assert waits == [1.0]


def test_sqlite_is_untouched_and_never_retried():
    database = store(backend="sqlite")
    initialize_with_dns_recovery(database, sleep=lambda _: pytest.fail("unexpected wait"))
    database.initialize.assert_called_once_with()


def test_bad_retry_budget_fails_before_database_write():
    database = store()
    for pauses in ((0,), (-1,), (31,), (1,) * 9):
        with pytest.raises(ValueError, match="retry budget"):
            initialize_with_dns_recovery(database, pauses=pauses)
    database.initialize.assert_not_called()
