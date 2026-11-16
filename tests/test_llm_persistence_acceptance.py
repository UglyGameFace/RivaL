"""LFM3 unattended acceptance must fail closed on stale or mismatched evidence."""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from parlay_bot.perpetuals.llm_persistence_acceptance import verified_receipt

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "tools" / "discloud_llm_persistence.py"
SPEC = importlib.util.spec_from_file_location("llm_discloud_receipt", FILE)
assert SPEC and SPEC.loader
RELAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELAY)


def example(now):
    model = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
    sha = "a" * 64
    result = SimpleNamespace(
        asset="BTC", model=model, evidence_sha256=sha,
        direction="uncertain", confidence="low", source_count=9,
    )
    row = {
        "asset": "BTC", "model": model, "evidence_sha256": sha,
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "direction": "uncertain", "confidence": "low",
        "source_count": 9, "thesis": "Historical disagreement",
        "risk": "Unverified future and costs",
    }
    return result, row


def test_current_exact_postgresql_row_is_verified():
    now = datetime.now(UTC)
    result, row = example(now)
    assert verified_receipt(result, row, now=now)


@pytest.mark.parametrize(("field", "bad_value"), [
    ("model", "unapproved"),
    ("evidence_sha256", "b" * 64),
    ("direction", "up"),
    ("confidence", "high"),
    ("source_count", 2),
    ("thesis", ""),
    ("risk", ""),
])
def test_mismatched_receipts_cannot_close_issue(field, bad_value):
    now = datetime.now(UTC)
    result, row = example(now)
    row[field] = bad_value
    assert not verified_receipt(result, row, now=now)


def test_stale_or_future_receipts_cannot_pass():
    now = datetime.now(UTC)
    result, row = example(now)
    row["issued_at"] = (now - timedelta(days=1)).isoformat()
    assert not verified_receipt(result, row, now=now)
    row["issued_at"] = (now + timedelta(minutes=2)).isoformat()
    assert not verified_receipt(result, row, now=now)
    assert not verified_receipt(result, None, now=now)


def test_off_host_result_requires_structured_response():
    for outcome in ("verified", "deferred", "failed"):
        expected = {"outcome": outcome}
        message = "Console ready\n" + RELAY.MARKER + json.dumps(expected) + "\n"
        assert RELAY.parse_receipt(message) == expected


@pytest.mark.parametrize("message", [
    "",
    'RIVAL_LFM3_PERSISTENCE_RECEIPT={"outcome":"claimed"}',
    'RIVAL_LFM3_PERSISTENCE_RECEIPT=["verified"]',
    "RIVAL_LFM3_PERSISTENCE_RECEIPT={invalid",
])
def test_invalid_gateway_receipts_fail_closed(message):
    with pytest.raises(ValueError):
        RELAY.parse_receipt(message)

def test_new_host_acceptance_requires_actual_math_evidence_and_fresh_model_row():
    valid = {
        "outcome": "verified", "model": "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M",
        "mathematical_evidence_status": "persisted_registry",
        "prospective_math_status": "insufficient",
        "cold_archive_readback": "not_performed_by_live_research",
        "evidence_hash_valid": True, "source_count": 9,
        "fresh_six_month_source_audits": 2, "from_cache": False,
        "saved_before": 3, "saved_after": 4,
    }
    assert RELAY.integrated_receipt(valid)
    for key, value in (
        ("outcome", "deferred"),
        ("model", "RivaL/verified-evidence-summary-v1"),
        ("mathematical_evidence_status", "unavailable"),
        ("prospective_math_status", "unavailable"),
        ("evidence_hash_valid", False),
        ("source_count", 2),
        ("from_cache", True),
        ("saved_after", 3),
    ):
        assert not RELAY.integrated_receipt({**valid, key: value})
    assert not RELAY.integrated_receipt({"outcome": "verified"})



def test_host_receipt_workflow_waits_for_verified_native_source():
    """Green skipped CI/release jobs never authorize an LFM3 consumption."""
    workflow = (ROOT / ".github" / "workflows" /
                "llm-persistence-acceptance.yml").read_text(encoding="utf-8")
    assert 'workflows: ["RivaL Production Release"]' in workflow
    assert 'if [[ "$GITHUB_EVENT_NAME" == "schedule" &&' in workflow
    assert "types: [completed]" in workflow
    assert 'RELEASE_SHA" != "$main_sha"' in workflow
    assert 'RELEASE_CONCLUSION" != "success"' in workflow
    assert 'parity_steps" != "1"' in workflow
    assert "Verify native Discloud source parity" in workflow
    assert "Deploy tested RivaL app without replacing its host environment" not in workflow
    assert 'integration_state="$(gh api "repos/$GITHUB_REPOSITORY/issues/261"' in workflow
    assert "if: steps.receipt.outputs.outcome == 'verified'" in workflow
    assert "push:\n" not in workflow


def test_host_console_diagnostic_never_exposes_stdout_or_stderr(monkeypatch, capsys):
    from urllib import request

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, *args):
            return b'{"apps":{"shell":{"stdout":"private console words", "stderr":"private error"}}}'

    monkeypatch.setenv("DISCLOUD_TOKEN", "test-token")
    monkeypatch.setenv("DISCLOUD_APP_ID", "test-app")
    monkeypatch.setattr(request, "urlopen", lambda *_args, **_kwargs: Response())
    assert RELAY.main() == 1
    observed = capsys.readouterr().out
    assert "structured_receipt" in observed and "absent" in observed
    assert "console_object_present" in observed
    assert "private console words" not in observed
    assert "private error" not in observed


def test_restart_deferral_is_retried_then_reported(monkeypatch):
    reports = iter([
        (1, {"outcome": "deferred", "reason": "insufficient_live_venues"}),
        (0, {"outcome": "verified"}),
    ])
    monkeypatch.setattr(RELAY, "_attempt", lambda: next(reports))
    monkeypatch.setattr(RELAY.time, "sleep", lambda _s: None)
    assert RELAY.main() == 0

    other = iter([(1, {"outcome": "deferred", "reason": "something_else"})])
    monkeypatch.setattr(RELAY, "_attempt", lambda: next(other))
    assert RELAY.main() == 1


def test_xrp_receipt_verified_only_for_matching_xrp_asset():
    now = datetime.now(UTC)
    result, row = example(now)
    result.asset = "XRP"
    row["asset"] = "XRP"
    assert verified_receipt(result, row, now=now, asset="XRP")
    # The default BTC receipt must not accept an XRP row, and an XRP
    # receipt must not accept a BTC row: the acceptance XRP ask exists
    # precisely so an XRP save regression cannot hide behind BTC.
    assert not verified_receipt(result, row, now=now)
    result.asset = "BTC"
    row["asset"] = "BTC"
    assert not verified_receipt(result, row, now=now, asset="XRP")


def test_split_console_acceptance_requires_both_assets(monkeypatch, capsys):
    """One valid BTC math proof and an independent exact XRP row are required."""
    btc = {
        "asset": "BTC", "outcome": "verified",
        "model": "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M",
        "mathematical_evidence_status": "persisted_registry",
        "prospective_math_status": "insufficient",
        "cold_archive_readback": "not_performed_by_live_research",
        "evidence_hash_valid": True,
        "source_count": 9, "fresh_six_month_source_audits": 2,
        "from_cache": False, "saved_before": 10, "saved_after": 11,
    }
    xrp = {
        "asset": "XRP", "outcome": "verified",
        "model": btc["model"], "source_count": 9,
        "xrp_verified": True,
        "from_cache": False, "saved_before": 11, "saved_after": 12,
    }
    calls = []
    def fetch(asset):
        calls.append(asset)
        return btc if asset == "BTC" else xrp

    monkeypatch.setattr(RELAY, "_console_once", fetch)
    code, receipt = RELAY._attempt()
    assert code == 0
    assert receipt["btc_math_new_db_verified"] is True
    assert receipt["xrp_exact_db_verified"] is True
    assert calls == ["BTC", "XRP"]
    assert "outcome" in capsys.readouterr().out

    # A missing or mismatched XRP record must never borrow BTC proof.
    for invalid in (
        {**xrp, "xrp_verified": False},
        {**xrp, "model": "different"},
        {**xrp, "saved_after": 11, "from_cache": False},
    ):
        calls.clear()
        monkeypatch.setattr(RELAY, "_console_once",
                            lambda asset, invalid=invalid: btc if asset == "BTC" else invalid)
        code, receipt = RELAY._attempt()
        assert code == 1
        assert receipt["outcome"] == "failed"
        assert calls == []


def test_invalid_btc_never_invokes_xrp(monkeypatch):
    calls = []
    def fetch(asset):
        calls.append(asset)
        return {"asset": "BTC", "outcome": "deferred",
                "reason": "minimum_spacing"}
    monkeypatch.setattr(RELAY, "_console_once", fetch)
    assert RELAY._attempt() == (1, {"asset": "BTC", "outcome": "deferred",
                                     "reason": "minimum_spacing"})
    assert calls == ["BTC"]


def test_host_scope_cli_preserves_legacy_both_and_independent_assets(monkeypatch, capsys):
    from parlay_bot.perpetuals import llm_persistence_acceptance as host
    scopes = []
    def btc(*, include_xrp):
        scopes.append("both" if include_xrp else "BTC")
        return {"outcome": "verified"}
    def xrp():
        scopes.append("XRP")
        return {"outcome": "verified"}

    monkeypatch.setattr(host, "verify_once", btc)
    monkeypatch.setattr(host, "verify_xrp_only", xrp)
    assert host.main(["--asset", "BTC"]) == 0
    assert host.main(["--asset", "XRP"]) == 0
    assert host.main(["--asset", "both"]) == 0
    assert scopes == ["BTC", "XRP", "both"]
    assert capsys.readouterr().out.count(host.MARKER) == 3


def test_gateway_error_exposes_only_status_code_not_private_body(monkeypatch, capsys):
    from urllib import error, request
    monkeypatch.setenv("DISCLOUD_TOKEN", "private-token")
    monkeypatch.setenv("DISCLOUD_APP_ID", "private-app")
    def fail(_req, timeout):
        assert timeout <= 150
        raise error.HTTPError(
            "https://internal.private.invalid/with-private-path",
            504, "PRIVATE SERVER MESSAGE", {}, None,
        )
    monkeypatch.setattr(request, "urlopen", fail)
    assert RELAY._console_once("BTC") is None
    out = capsys.readouterr().out
    assert '"http_status": 504' in out
    assert "PRIVATE SERVER MESSAGE" not in out
    assert "internal.private" not in out
    assert "private-token" not in out
    assert "private-app" not in out


def test_xrp_spacing_retry_never_replays_verified_btc(monkeypatch):
    model = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
    btc = {"asset": "BTC", "outcome": "verified", "model": model}
    pending = {"asset": "XRP", "outcome": "deferred", "reason": "minimum_spacing"}
    xrp = {"asset": "XRP", "outcome": "verified", "model": model,
           "xrp_verified": True, "source_count": 9, "from_cache": False,
           "saved_before": 10, "saved_after": 11}
    calls, slept = [], []

    def fetch(asset):
        calls.append(asset)
        if asset == "BTC":
            return btc
        return pending if calls.count("XRP") == 1 else xrp

    monkeypatch.setattr(RELAY, "integrated_receipt",
                        lambda report: report is btc)
    monkeypatch.setattr(RELAY, "_console_once", fetch)
    monkeypatch.setattr(RELAY.time, "sleep", slept.append)
    status, receipt = RELAY._attempt()
    assert status == 0
    assert receipt["outcome"] == "verified"
    assert calls == ["BTC", "XRP", "XRP"]
    assert slept == [RELAY.RESTART_WAIT_SECONDS]


@pytest.mark.parametrize("standalone", [True, False])
def test_shared_xrp_stage_has_same_exact_persistence_gates(monkeypatch, standalone):
    from parlay_bot.perpetuals import llm_persistence_acceptance as host

    class Store:
        def __init__(self, counts):
            self.counts = iter(counts)

        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return self

        def fetchone(self):
            return {"n": next(self.counts)}

    now = datetime.now(UTC)
    result, row = example(now)
    result.asset = row["asset"] = "XRP"
    result.from_cache = False
    calls = []

    async def ask(_market, _settings, *, asset):
        calls.append(asset)
        return result, 1.25

    monkeypatch.setattr(host, "_ask", ask)
    market = SimpleNamespace(cached_ai_research=lambda **_kw: row)
    report = host._verify_xrp_stage(
        market, SimpleNamespace(), Store([3, 4]),
        {"outcome": "deferred", "asset": "XRP"}, standalone=standalone,
    )
    assert report["outcome"] == "verified"
    assert report["xrp_verified"] is True
    assert calls == ["XRP"]
    assert ("saved_after" in report) is standalone

    # A cache miss without a new DB row is never accepted as persistence.
    bad = host._verify_xrp_stage(
        market, SimpleNamespace(), Store([4, 4]),
        {"outcome": "deferred", "asset": "XRP"}, standalone=standalone,
    )
    assert bad["outcome"] == "failed"
    assert bad["reason"] == "xrp_exact_database_receipt_missing"
