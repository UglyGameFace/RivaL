"""Regression coverage for rejected XMarket's train-only ablation receipt."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from parlay_bot.research import xmarket_feature_search as research
from parlay_bot.research.xmarket_features import feature_names


def _training_rows(n=9600):
    rng = np.random.default_rng(20261116)
    names = feature_names(("bitget", "okx"))
    values = rng.normal(size=(n, len(names)))
    # Real-valued synthetic labels; this never supplies a trading candidate.
    signal = values[:, names.index("pm_up")] - .5 * values[:, names.index("pm_up_chg_3")]
    probability = 1 / (1 + np.exp(-signal))
    frame = pd.DataFrame(values, columns=names,
        index=pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"))
    frame["y"] = (rng.random(n) < probability).astype(float)
    return frame, names


def test_rejected_scope_reports_predeclared_purged_train_only_profiles():
    frame, names = _training_rows()
    result = research.compare_train_only(frame, names=names, horizon_minutes=60)
    assert result["research_only"] is True
    assert result["can_publish_model"] is False
    assert result["uses_outer_validation_or_test"] is False
    assert result["status"] == "measured_train_only"
    assert [r["name"] for r in result["profiles"]] == [
        "full", "no_dispersion", "no_market_momentum",
        "native_levels_plus_candles",
    ]
    assert all(r["target_count"] == result["fold"]["eval_targets"]
               for r in result["profiles"])
    assert all(0 <= r["brier_by_target"] <= 1 for r in result["profiles"])
    assert result["profiles"][0]["brier_delta_vs_full"] == 0
    assert pd.Timestamp(result["fold"]["fit_latest"]) < (
        pd.Timestamp(result["fold"]["eval_earliest"]) - pd.Timedelta(minutes=60)
    )
    assert "weights" not in str(result).lower()
    assert "suggested_winner" not in result


def test_duplicate_source_rows_cannot_inflate_distinct_evaluation_targets(monkeypatch):
    frame, names = _training_rows()
    from parlay_bot.research import archive_training

    # Deterministic constant predictions isolate the receipt aggregation from
    # the research-only model fitting, and keep the test fast.
    monkeypatch.setattr(archive_training, "_fit_logistic",
                        lambda x, y: np.zeros(x.shape[1] + 1))
    first = research.compare_train_only(frame, names=names, horizon_minutes=5)
    duplicated = pd.concat((frame, frame.iloc[-100:])).sort_index()
    again = research.compare_train_only(duplicated, names=names, horizon_minutes=5)
    assert first["status"] == again["status"] == "measured_train_only"
    assert first["fold"]["eval_targets"] == again["fold"]["eval_targets"]
    assert [r["brier_by_target"] for r in first["profiles"]] == [
        r["brier_by_target"] for r in again["profiles"]
    ]
    assert again["fold"]["eval_rows"] > first["fold"]["eval_rows"]


def test_insufficient_distinct_train_targets_refuse_fitting(monkeypatch):
    frame, names = _training_rows(600)
    from parlay_bot.research import archive_training

    def forbidden(*args, **kwargs):
        raise AssertionError("no fitting permitted on a short fold")
    monkeypatch.setattr(archive_training, "_fit_logistic", forbidden)
    report = research.compare_train_only(frame, names=names, horizon_minutes=60)
    assert report["status"] == "insufficient_train_only_fold"
    assert not report["profiles"]
    assert report["can_publish_model"] is False


@pytest.mark.parametrize("horizon", (0, 10, 120))
def test_unsupported_horizons_are_rejected(horizon):
    frame, names = _training_rows(10)
    with pytest.raises(ValueError, match="unsupported horizon"):
        research.compare_train_only(frame, names=names, horizon_minutes=horizon)


def test_nonfinite_or_ungraded_rows_fail_closed():
    frame, names = _training_rows()
    frame.iloc[0, 0] = float("nan")
    with pytest.raises(ValueError, match="complete original features"):
        research.compare_train_only(frame, names=names, horizon_minutes=5)
    frame.iloc[0, 0] = 0.0
    frame.iloc[0, -1] = 2.0
    with pytest.raises(ValueError, match="research labels"):
        research.compare_train_only(frame, names=names, horizon_minutes=5)
