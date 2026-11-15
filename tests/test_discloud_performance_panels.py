"""Deployment proof requires the exact renderer and real, complete lifecycle reads."""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tools import discloud_performance_panels as probe


def _report():
    return {
        "source_sha256": {"home.py": "a" * 64}, "read_only": True,
        "sends_discord_messages": False, "proves_edge": False,
        "panels": {key: {"embeds": 1, "characters": 1700,
                         "all_timeframes_visible": True, "ledger_counts_match": True}
                   for key in ("overview", "models")},
        "scopes": [{"asset": a, "horizon_minutes": h, "usable_imports": 1,
                    "issued_24h": 26, "settled_24h": 8, "private_document": "never print"}
                   for a in ("BTC", "ETH", "SOL", "XRP") for h in (5, 15, 60)],
    }


def test_console_result_is_allowlisted_and_cannot_claim_a_deployment_from_stale_source():
    report = _report()
    clean = probe.checked_report(probe.MARKER + json.dumps(report), report["source_sha256"])
    assert "never print" not in json.dumps(clean)
    assert clean["proves_edge"] is False
    with pytest.raises(ValueError):
        probe.checked_report(probe.MARKER + json.dumps(report), {"home.py": "b" * 64})
    with pytest.raises(ValueError):
        probe.checked_report("no marker", report["source_sha256"])
    with pytest.raises(ValueError):
        probe.checked_report((probe.MARKER + json.dumps(report) + "\n") * 2,
                             report["source_sha256"])


@pytest.mark.parametrize("problem", ["missing_scope", "no_xmarket", "count_mismatch", "write"])
def test_incomplete_render_or_mutating_probe_cannot_pass(problem):
    report = copy.deepcopy(_report())
    if problem == "missing_scope":
        report["scopes"].pop()
    elif problem == "no_xmarket":
        report["panels"]["overview"]["all_timeframes_visible"] = False
    elif problem == "count_mismatch":
        report["panels"]["models"]["ledger_counts_match"] = False
    else:
        report["read_only"] = False
    with pytest.raises(ValueError):
        probe.checked_report(probe.MARKER + json.dumps(report), report["source_sha256"])


def test_api_failures_never_echo_secrets_or_remote_stderr(monkeypatch, capsys):
    monkeypatch.setenv("DISCLOUD_TOKEN", "private-token")
    monkeypatch.setenv("DISCLOUD_APP_ID", "private-app")

    def fail(req, timeout):
        assert req.get_method() == "PUT" and req.full_url.endswith("/console")
        raise RuntimeError("private-token postgres://credentials")

    monkeypatch.setattr(probe.request, "urlopen", fail)
    assert probe.main() == 1
    text = capsys.readouterr().out
    assert "RuntimeError" in text and "private-token" not in text and "postgres" not in text


def test_probe_renders_actual_views_through_read_only_connections(learning_store, monkeypatch):
    from parlay_bot import config
    from parlay_bot.storage import hot
    from tests.test_xmarket_status import _setup, _shadow

    _setup(learning_store)
    _shadow(learning_store)
    attempts = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            attempts.append(sql.strip().split()[0].upper())
            assert attempts[-1] in ("SELECT", "WITH")
            return self.conn.execute(sql, params)

    class ReadOnly:
        def __getattr__(self, name):
            return getattr(learning_store, name)

        @contextmanager
        def connect(self):
            with learning_store.connect() as conn:
                yield Connection(conn)

        def close(self):
            pass

    def connect(_dsn, *, read_only):
        assert read_only is True
        return ReadOnly()

    monkeypatch.setattr(hot, "PostgresHotStore", connect)
    monkeypatch.setattr(config, "Settings", lambda: SimpleNamespace(
        rival_database_url=SimpleNamespace(get_secret_value=lambda: "private-dsn")))
    report = probe.host_report()
    assert set(attempts) <= {"SELECT", "WITH"}
    accepted = probe.checked_report(probe.MARKER + json.dumps(report), report["source_sha256"])
    assert len(accepted["scopes"]) == 12
    assert all(p["ledger_counts_match"] for p in accepted["panels"].values())
