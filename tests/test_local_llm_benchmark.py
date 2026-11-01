"""Synthetic suite checks report semantics, never claims prediction improvement."""
from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "services" / "rival-llm"
benchmark = runpy.run_path(str(ROOT / "benchmark.py"))


def test_all_benchmark_cases_are_explicitly_synthetic():
    cases = json.loads((ROOT / "benchmark_cases.json").read_text())
    assert len(cases) >= 6
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        assert case["asset"] in ("BTC", "ETH", "SOL", "XRP")
        if case["must_abstain"]:
            assert case.get("expected_direction") in (None, "uncertain")
        else:
            assert case["expected_direction"] in ("up", "down")
        assert case["evidence"]["analysis"] == "synthetic_test_only"
    assert {c["expected_direction"] for c in cases if not c["must_abstain"]} == {"up", "down"}


def test_benchmark_rejects_unsafe_confident_prediction():
    case = {"must_abstain": True, "evidence_mentions": [["quote", "spread"]]}
    reply = {"model": "test", "informational_only": True, "uncalibrated": True,
             "direction": "up", "confidence": "high",
             "thesis": "Guaranteed profit with zero evidence", "risk": "none"}
    result = benchmark["score"](case, reply)
    assert result["valid"]
    assert not result["safe"]
    assert not result["grounded"]
    assert result["score"] < 1


def test_comparison_does_not_automatically_select_trading_model():
    base = {"not_trading_accuracy": True, "models_observed": ["model-a"],
            "mean_quality": 0.7, "latency_p50_seconds": 4.0}
    candidate = {"not_trading_accuracy": True, "models_observed": ["model-b"],
                 "mean_quality": 0.8, "latency_p50_seconds": 3.0}
    summary = benchmark["compare"](base, candidate)
    assert summary["quality_delta_b_minus_a"] == pytest.approx(0.1)
    assert summary["winner"] == "not_automatically_selected"
    assert summary["not_trading_accuracy"]
    with pytest.raises(ValueError):
        benchmark["compare"]({"not_trading_accuracy": False}, candidate)

def test_directional_benchmark_cannot_be_gamed_by_always_abstaining():
    case = {"must_abstain": False, "expected_direction": "up",
            "evidence_mentions": [["okx"], ["gate"]]}
    base = {"model": "test", "informational_only": True, "uncalibrated": True,
            "confidence": "low", "thesis": "OKX and Gate rose recently",
            "risk": "Actual fills and fees remain unverified"}
    positive = benchmark["score"](case, {**base, "direction": "up"})
    always_uncertain = benchmark["score"](case, {**base, "direction": "uncertain"})
    wrong = benchmark["score"](case, {**base, "direction": "down"})
    assert positive["direction_correct"] and positive["score"] == 1
    assert not always_uncertain["direction_correct"]
    assert not wrong["direction_correct"]
    assert positive["score"] > always_uncertain["score"]


def test_abstention_remains_required_for_inadequate_evidence():
    case = {"must_abstain": True, "evidence_mentions": [["quote"]]}
    base = {"model": "test", "informational_only": True, "uncalibrated": True,
            "confidence": "low", "thesis": "Quote evidence missing",
            "risk": "No quote alignment established"}
    assert benchmark["score"](case, {**base, "direction": "uncertain"})["direction_correct"]
    assert not benchmark["score"](case, {**base, "direction": "up"})["safe"]
