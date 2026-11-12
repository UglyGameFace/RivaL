"""Research-only 24h/7d/30d variance-blend and separate-target evaluation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from parlay_bot.research.volatility_blend_research import CANDIDATES, evaluate_blend

START = datetime(2026, 8, 1, tzinfo=UTC)
VALIDATION = START + timedelta(days=40)
TEST = START + timedelta(days=68)


def _frames(*, days=100, seed=42):
    rng = np.random.default_rng(seed)
    n = days * 288
    changes = rng.normal(0, 0.0017, n)
    changes[n//2:] *= 1.4
    price = 200 * np.exp(np.cumsum(changes))
    index = pd.date_range(START, periods=n, freq="5min", tz="UTC")
    k = pd.DataFrame({"close": price}, index=index)
    return {"bitget|BTC": (k, None)}


@pytest.mark.parametrize("horizon", (5, 15, 60))
def test_blend_tests_fixed_validated_weights_on_separate_future_targets(horizon):
    report = evaluate_blend(
        _frames(), horizon=horizon, train_end=VALIDATION, test_start=TEST)
    assert report["status"] == "evaluated_offline_not_promotable"
    assert report["usable_sources"] == 1
    assert report["test"]["days"] >= 28
    assert report["validation"]["days"] >= 25
    assert tuple(report["selected_weights_24h_7d_30d"]) in CANDIDATES
    assert report["holdout"]["baseline_24h"]["nll"] is not None
    assert report["holdout"]["selected"]["nll"] is not None
    assert report["holdout"]["nll_gain_vs_24h"] == pytest.approx(
        report["holdout"]["baseline_24h"]["nll"]
        - report["holdout"]["selected"]["nll"], abs=1e-7)
    tasks = report["separate_targets"]
    assert set(tasks) == {"direction", "move_size", "timing"}
    assert tasks["direction"]["graded_targets"] == report["test"]["rows"]
    assert tasks["direction"]["direction_model_tested"] is False
    assert tasks["timing"]["timing_model_tested"] is False
    assert tasks["timing"]["intrabar_first_passage_inferred"] is False
    assert 0 <= tasks["move_size"]["selected_blend_interval_coverage"] <= 1
    assert 0 <= tasks["timing"]["hit_fraction"] <= 1
    assert report["original_uploaded_parameters_used"] is False
    assert report["qualified_for_live_use"] is False
    assert report["proven_after_cost_edge"] is False
    assert report["not_independent_samples"] is True


def test_blend_selects_without_seeing_test_period_prices():
    frame = _frames()
    old = evaluate_blend(frame, horizon=60, train_end=VALIDATION, test_start=TEST)
    new_frame = {k: (v[0].copy(), v[1]) for k, v in frame.items()}
    for candles, _ in new_frame.values():
        candles.loc[candles.index >= TEST, "close"] *= 1.45
    new = evaluate_blend(new_frame, horizon=60, train_end=VALIDATION, test_start=TEST)
    assert new["selected_weights_24h_7d_30d"] == old["selected_weights_24h_7d_30d"]
    assert new["validation_nll_selected"] == old["validation_nll_selected"]
    assert new["test"]["rows"] == old["test"]["rows"]
    assert new["holdout"]["selected"]["nll"] != old["holdout"]["selected"]["nll"]


def test_missing_or_gapped_native_history_cannot_claim_a_blended_model():
    short = evaluate_blend(
        _frames(days=29), horizon=5, train_end=VALIDATION, test_start=TEST)
    assert short["status"] == "missing_contiguous_30d_source_history"
    assert short["qualified_for_live_use"] is False

    frames = _frames()
    candles = frames["bitget|BTC"][0]
    frames["bitget|BTC"] = (candles.drop(candles.index[::1000]), None)
    gapped = evaluate_blend(
        frames, horizon=5, train_end=VALIDATION, test_start=TEST)
    assert gapped["status"] == "missing_contiguous_30d_source_history"


def test_prohibits_naive_windows_and_non_five_minute_horizons():
    with pytest.raises(ValueError):
        evaluate_blend(_frames(), horizon=30, train_end=VALIDATION, test_start=TEST)
    with pytest.raises(ValueError):
        evaluate_blend(
            _frames(), horizon=5, train_end=VALIDATION.replace(tzinfo=None),
            test_start=TEST)
