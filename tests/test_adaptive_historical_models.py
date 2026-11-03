"""Training-only probability improvements with a sealed final holdout."""
from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research import live_training


def _samples(start: datetime, count: int, *, reverse_labels: bool = False):
    result = []
    for i in range(count):
        opened = start + timedelta(hours=i)
        closed = opened + timedelta(minutes=5)
        # Deterministic synthetic signals; the real training workflow uses
        # original SHA-verified 5-minute history, not these test observations.
        features = (
            ((i * 7) % 17 - 8) / 8,
            ((i * 11) % 19 - 9) / 9,
            ((i * 13) % 23) / 23,
            ((i * 5) % 29) / 29,
            ((i * 3) % 7 - 3) / 3,
            ((i * 17) % 31 - 15) / 15,
            ((i * 2) % 11 - 5) / 5,
            ((i * 19) % 37 - 18) / 18,
            ((i * 23) % 41) / 41,
            ((i * 29) % 43 - 21) / 21,
        )
        label = int(features[0] + .6 * features[1] > 0)
        if reverse_labels:
            label = 1 - label
        result.append((opened, closed, features, label))
    return result


def test_shrinkage_keeps_live_feature_contract_and_bounded_probabilities():
    weights = [.6, *([.2] * 10)]
    base = .41
    prior = math.log(base / (1 - base))
    for alpha in (.25, .5, .75):
        revised = live_training._shrink_logit(weights, base=base, fraction=alpha)
        assert len(revised) == 11
        assert revised[0] == pytest.approx(prior + alpha * (weights[0] - prior))
        assert revised[1:] == pytest.approx([alpha * w for w in weights[1:]])
        assert all(math.isfinite(w) for w in revised)
    with pytest.raises(ValueError):
        live_training._shrink_logit(weights, base=base, fraction=0)
    with pytest.raises(ValueError):
        live_training._shrink_logit(weights[:5], base=base, fraction=.5)


def test_adaptive_training_is_reproducible_and_uses_training_labels_only():
    train = _samples(datetime(2026, 5, 1, tzinfo=UTC), 540)
    weights_a, prior_a = live_training._fit_adagrad(train, l2=.001)
    weights_b, prior_b = live_training._fit_adagrad(train, l2=.001)
    assert weights_a == weights_b and prior_a == prior_b
    assert len(weights_a) == 11 and all(math.isfinite(w) for w in weights_a)
    assert 0 < prior_a < 1
    scores = live_training._score(train, weights_a, prior_a)
    assert scores["count"] == 540
    assert scores["brier"] < scores["baseline_brier"]
    with pytest.raises(ValueError):
        live_training._fit_adagrad(train, l2=.2)


def test_new_family_selection_never_reads_sealed_test_outcomes(monkeypatch):
    start = datetime(2026, 5, 1, tzinfo=UTC)
    train = _samples(start, 500)
    val = _samples(start + timedelta(days=25), 180)
    test = _samples(start + timedelta(days=35), 180)
    inverted_test = [
        (opened, closed, features, 1 - label)
        for opened, closed, features, label in test
    ]
    months = [f"2026-{month:02d}" for month in range(5, 11)]
    identity = {
        "venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT",
        "interval_minutes": 5,
    }
    provenance = {
        "months": months,
        "source_sha256_by_month": {
            month: hashlib.sha256(month.encode()).hexdigest() for month in months
        },
    }

    def run(held_out):
        monkeypatch.setattr(
            live_training, "compatible_partitions",
            lambda rows, vectors, *, horizon, identity: (train, val, held_out),
        )
        return live_training.evaluate_compatible(
            [], horizon=5, prepared=(identity, [], provenance, {}),
        )

    first = run(test)
    second = run(inverted_test)
    assert first["model_sha256"] == second["model_sha256"]
    assert first["model_spec"] == second["model_spec"]
    assert first["validation"] == second["validation"]
    assert first["out_of_sample_test"] != second["out_of_sample_test"]
    compared = first["compared_on_validation"]
    assert len(compared) >= 13
    assert any(c["method"] == "recent-training-adaptive-logit" for c in compared)
    assert any(c["method"] == "training-prior-shrink-0.5" for c in compared)
    assert any(c["method"] == "adaptive-logit-l2-0.001" for c in compared)
    assert first["validation"]["brier"] == min(
        c["scores"]["brier"] for c in compared
    )
    assert len(first["model_spec"]["weights"]) == 11
    assert 300 <= first["model_spec"]["training_count"] <= len(train)
    assert first["live_model_promoted"] is False
    assert first["prospective_accuracy"] is None
