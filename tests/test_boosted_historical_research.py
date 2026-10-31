"""Sealed test and source-identity controls for boosted historical research."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.boosted_models import evaluate_compatible_boosters
from parlay_bot.research.live_training import compatible_partitions


class ConstantModel:
    def __init__(self, probability):
        self.probability = probability
        self.fit_count = 0

    def fit(self, features, labels):
        assert features and len(features) == len(labels)
        self.fit_count = len(labels)
        return self

    def predict_proba(self, features):
        return [[1 - self.probability, self.probability] for _ in features]


def _prepared():
    rows = []
    vectors = {}
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(1200):
        stamp = opened + timedelta(minutes=5 * index)
        price = (100 + .01 * index if index < 900
                 else 109 - .02 * (index - 900))
        rows.append({
            "opened_at": stamp.isoformat(),
            "closed_at": (stamp + timedelta(minutes=5, milliseconds=-1)).isoformat(),
            "close": price,
        })
        if index >= 39:
            vectors[index] = (index % 11 / 10,) * 10
    identity = {
        "venue": "binance", "contract": "BTCUSDT",
        "quote": "USDT", "interval_minutes": 5,
    }
    provenance = {
        "months": ["2026-01", "2026-02", "2026-03",
                   "2026-04", "2026-05", "2026-06"],
        "source_sha256_by_month": {"2026-01": "a" * 64},
    }
    return identity, rows, provenance, vectors


def test_purged_partitions_never_train_on_validation_or_test_outcomes():
    identity, rows, _, vectors = _prepared()
    train, validation, test = compatible_partitions(
        rows, vectors, horizon=5, identity=identity,
    )
    assert len(train) >= 300
    assert min(len(validation), len(test)) >= 100
    assert train[-1][1] < validation[0][0]
    assert validation[-1][1] < test[0][0]


def test_validation_only_model_selection_and_sealed_final_test():
    prepared = _prepared()
    upper = ConstantModel(.9)
    lower = ConstantModel(.1)
    result = evaluate_compatible_boosters(
        [], horizon=5, prepared=prepared,
        estimators={
            "hist_gradient_boosting": lambda: upper,
            "lightgbm": lambda: lower,
        },
    )
    assert result["status"] == "evaluated"
    assert result["model_spec"]["family"] == "hist_gradient_boosting"
    assert upper.fit_count > 300 and lower.fit_count == upper.fit_count
    assert result["validation"]["brier"] < result["out_of_sample_test"]["brier"]
    assert result["live_model_promoted"] is False
    assert result["eligible_for_live_import"] is False
    assert result["research_candidate_passed"] is False
    assert len(result["model_sha256"]) == 64


def test_unavailable_ml_libraries_are_not_claimed_as_fitted():
    result = evaluate_compatible_boosters(
        [], horizon=5, prepared=_prepared(), estimators={},
    )
    assert result["status"] == "dependencies_missing"
    assert result["live_model_promoted"] is False


def test_requires_exact_supported_source_interval_and_horizon():
    identity, rows, _, vectors = _prepared()
    with pytest.raises(ValueError):
        compatible_partitions(
            rows, vectors, horizon=7, identity=identity,
        )
