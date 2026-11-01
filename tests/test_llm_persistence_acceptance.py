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
