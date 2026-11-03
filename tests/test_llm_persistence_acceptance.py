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



def test_host_receipt_workflow_waits_for_actual_deployment():
    """Green skipped CI/release jobs never authorize an LFM3 consumption."""
    workflow = (ROOT / ".github" / "workflows" /
                "llm-persistence-acceptance.yml").read_text(encoding="utf-8")
    assert 'workflows: ["RivaL Production Release"]' in workflow
    assert 'if [[ "$GITHUB_EVENT_NAME" == "schedule" &&' in workflow
    assert "types: [completed]" in workflow
    assert 'RELEASE_SHA" != "$main_sha"' in workflow
    assert 'RELEASE_CONCLUSION" != "success"' in workflow
    assert 'deploy_steps" != "1"' in workflow
    assert "Deploy tested RivaL app without replacing its host environment" in workflow
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
