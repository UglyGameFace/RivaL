"""The ZIP-derived feature groups need same-outcome comparisons, not a leaderboard
that accidentally ranks BTC compression against XRP directional change.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research import volcal_group_research as research
from tests.test_volcal_training import _frames, _provenance

ANCHOR = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _report(asset="BTC", horizon=5, group=None):
    return {
        "family": "volcal_v1", "feature_contract": "volcal-candle-vol-v1",
        "asset": asset, "horizon_minutes": horizon, "gates_passed": True,
        **({"feature_groups_off": [group]} if group else {}),
        "evaluation": {
            "train_rows": 300, "train_before": "2026-09-01", "test_from": "2026-10-01",
            "validation": {"rows": 200, "days": 12, "brier_gain": .012},
            "test": {"rows": 180, "days": 11, "brier_gain": .016},
        },
    }


def test_group_rotation_covers_each_group_per_asset_without_results_based_selection():
    for asset in research.ASSET_ORDER:
        choices = [
            research.scheduled_group(asset=asset, trained_through=ANCHOR + timedelta(days=7 * i))
            for i in range(4)
        ]
        assert len(set(choices)) == 4
        assert set(choices) == set(research.GROUP_ORDER)
        assert choices == [
            research.scheduled_group(asset=asset, trained_through=ANCHOR + timedelta(days=7 * i))
            for i in range(4)
        ]
    with pytest.raises(ValueError, match="unsupported"):
        research.scheduled_group(asset="DOGE", trained_through=ANCHOR)
    with pytest.raises(ValueError, match="timezone-aware"):
        research.scheduled_group(asset="BTC", trained_through=ANCHOR.replace(tzinfo=None))


def test_paired_brier_difference_is_only_descriptive_and_same_scope():
    group = research.scheduled_group(asset="BTC", trained_through=ANCHOR)
    full, without = _report(), _report(group=group)
    without["evaluation"]["test"]["brier_gain"] = .007
    paired = research.compare(
        full, without, asset="BTC", horizon=5, trained_through=ANCHOR)
    assert paired["status"] == "paired_descriptive_research_only"
    assert paired["test_gain_from_including_group"] == pytest.approx(.009)
    assert paired["test_rows"] == 180
    assert paired["same_asset_horizon_source_and_test_rows"] is True
    assert paired["prospective_forward_edge_proven"] is False
    assert paired["after_cost_edge_proven"] is False
    assert paired["statistically_significant_superiority_proven"] is False
    assert paired["promotable_from_this_result"] is False

    wrong_asset = _report(asset="ETH", group=group)
    with pytest.raises(ValueError, match="cross-scope"):
        research.compare(full, wrong_asset, asset="BTC", horizon=5, trained_through=ANCHOR)
    without["evaluation"]["test"]["rows"] = 179
    with pytest.raises(ValueError, match="not paired"):
        research.compare(full, without, asset="BTC", horizon=5, trained_through=ANCHOR)
    without["evaluation"]["test"]["rows"] = 180
    without["evaluation"]["train_before"] = "2026-08-01"
    with pytest.raises(ValueError, match="same train/test"):
        research.compare(full, without, asset="BTC", horizon=5, trained_through=ANCHOR)


def test_blocked_full_fit_skips_extra_model_work(monkeypatch):
    from parlay_bot.research import volcal_training

    def forbid(*args, **kwargs):
        raise AssertionError("blocked full model cannot trigger another fit")

    monkeypatch.setattr(volcal_training, "train_run", forbid)
    status = research.evaluate({}, {"asset": "BTC", "horizon": 5,
        "trained_through": ANCHOR}, {"gates_passed": False})
    assert status["status"] == "full_model_not_evaluable"
    assert status["promotable_from_this_result"] is False


def test_paired_ablation_refits_same_asset_horizon_and_omits_second_blend():
    """Real synthetic archive frame and Platt fit, not a mocked score."""
    from parlay_bot.research import volcal_training

    frames = _frames(n=3800, rho=.3)
    k = next(iter(frames.values()))[0]
    training = {"asset": "BTC", "horizon": 5,
        "train_end": k.index[2400], "test_start": k.index[3100],
        "trained_through": k.index[-1],
        "provenance": _provenance(), "code_version": "synthetic-test"}
    full = volcal_training.train_run(
        frames, **training, include_research_diagnostics=False)
    actual = research.evaluate(frames, training, full["report"])
    group = research.scheduled_group(asset="BTC", trained_through=k.index[-1])
    assert actual["schema"] == research.SCHEMA
    assert actual["group"] == group
    assert actual["status"] == "paired_descriptive_research_only"
    assert actual["test_rows"] > 100 and actual["validation_rows"] > 100
    assert actual["test_gain_from_including_group"] == pytest.approx(
        actual["test_brier_gain_full"] - actual["test_brier_gain_without_group"],
        abs=1e-6)
    # No artifact may be manufactured or promoted by this research.
    assert actual["model_artifact_published_by_research"] is False
    assert actual["promotable_from_this_result"] is False
