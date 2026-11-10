"""RivaL Intelligence's stdlib Sentry reporter: valid envelopes, scrubbed, deduped."""
from __future__ import annotations

import json
import logging
import runpy
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "services" / "rival-llm"
sys.path.insert(0, str(ROOT))
MOD = runpy.run_path(str(ROOT / "error_report.py"))


def test_install_never_reports_under_tests_even_with_explicit_dsn(monkeypatch):
    """Audit step 4 item 2: an exported RIVAL_LLM_SENTRY_DSN used to defeat
    the pytest guard, so a test run reported to the rival-intelligence
    project as production issues (same hole as observability.init_sentry)."""
    monkeypatch.setenv("RIVAL_LLM_SENTRY_DSN", MOD["DEFAULT_DSN"])
    handler = MOD["install"]()
    try:
        assert handler is None
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)


def test_dsn_parses_to_the_envelope_endpoint():
    url, auth = MOD["parse_dsn"](MOD["DEFAULT_DSN"])
    assert url == "https://o4512385053097984.ingest.us.sentry.io/api/4512385076625408/envelope/"
    assert "sentry_key=76f3dc94f8d48c788d7b0fad78d129db" in auth
    assert MOD["parse_dsn"]("not a dsn") is None


def test_errors_with_tracebacks_are_sent_once_and_scrubbed():
    sent = []
    handler = MOD["SentryHandler"](MOD["DEFAULT_DSN"], sender=sent.append)
    log = logging.getLogger("rival-llm-test")
    log.addHandler(handler)
    log.propagate = False
    try:
        try:
            raise RuntimeError("backend died token=abc123")
        except RuntimeError:
            log.exception("RivaL Intelligence stopped")
            log.exception("RivaL Intelligence stopped")  # duplicate within 10 min
        log.info("ordinary info line")                     # never sent
        log.warning("memory_receipt current_mb=2300 limit_mb=2450")
        deadline = time.monotonic() + 3
        while len(sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        log.removeHandler(handler)
    assert len(sent) == 2
    header, item, body = sent[0].rstrip(b"\n").split(b"\n")
    event = json.loads(body)
    assert json.loads(item)["type"] == "event" and json.loads(header)["event_id"]
    exc = event["exception"]["values"][0]
    assert exc["type"] == "RuntimeError" and "abc123" not in exc["value"]
    assert exc["stacktrace"]["frames"][-1]["function"].startswith("test_")
    assert json.loads(sent[1].rstrip(b"\n").split(b"\n")[2])["level"] == "warning"
