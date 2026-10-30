"""Regimes must be stamped before outcomes and keep correlation honest."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.regime_evidence import (
    VERSION,
    evaluate_regimes,
    issue_regime,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_perpetuals_forward_accuracy import seed


def _rows(*, wrong_week: bool = False, duplicate_venues: int = 1) -> list[dict]:
    start = datetime(2026, 10, 5, tzinfo=UTC)  # Monday
    result = []
    for regime in ("quiet", "turbulent"):
        for day in range(14):
            for slot in range(7):
                target = start + timedelta(
                    days=day, minutes=5 * (slot + (40 if regime == "quiet" else 80))
                )
                negative = wrong_week and regime == "turbulent" and day >= 7
                model = 0.20 if negative else 0.07
                for _ in range(duplicate_venues):
                    result.append({
                        "target_opened_at": target.isoformat(),
                        "issued_at": (target - timedelta(minutes=5)).isoformat(),
                        "settled_at": (target + timedelta(minutes=6)).isoformat(),
                        "regime": regime,
                        "model_brier": model,
                        "baseline_brier": 0.14,
                    })
    return result


def test_issue_regime_uses_only_predeclared_causal_features():
    assert issue_regime((0, 0, 0.05, 0, 0)) == "quiet"
    assert issue_regime((0, 0, 0.10, 0, 0)) == "normal"
    assert issue_regime((0, 0, 0.36, 0, 0)) == "turbulent"
    with pytest.raises(ValueError):
        issue_regime((0, 0, float("nan"), 0, 0))


def test_regime_score_requires_repeatable_skill_in_distinct_future_conditions():
    evaluation = evaluate_regimes(
        _rows(), as_of=datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert evaluation["status"] == "consistent_shadow_skill_research_only"
    assert evaluation["unique_target_windows"] == 196
    assert evaluation["regimes"]["quiet"]["qualified_weeks"] == 2
    assert evaluation["regimes"]["turbulent"]["qualified_weeks"] == 2
    assert evaluation["regimes"]["quiet"]["paired_brier_gain"] == pytest.approx(0.07)
    assert not evaluation["actual_execution_costs_measured"]
    assert not evaluation["strategy_profitability_verified"]
    assert not evaluation["trading_approved"]
    many = evaluate_regimes(
        _rows(duplicate_venues=10), as_of=datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert many == evaluation  # Ten exchange reports are not ten trials.


def test_one_bad_market_regime_rejects_pooled_winner():
    result = evaluate_regimes(
        _rows(wrong_week=True), as_of=datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert result["regimes"]["quiet"]["status"] == "stable_shadow_gain"
    assert result["regimes"]["turbulent"]["status"] == "unstable_or_negative_skill"
    assert result["status"] == "regime_instability_detected"


def test_ambiguous_regime_and_early_or_unsettled_forecasts_do_not_count():
    rows = _rows()
    contradict = dict(rows[0], regime="normal")
    future_seen = dict(rows[1], issued_at=(
        datetime.fromisoformat(rows[1]["target_opened_at"])
        + timedelta(minutes=6)
    ).isoformat())
    rows.extend((contradict, future_seen))
    result = evaluate_regimes(rows, as_of=datetime(2026, 10, 28, tzinfo=UTC))
    assert result["disputed_regime_targets"] == 1
    assert result["unique_target_windows"] == 195
    assert result["status"] == "consistent_shadow_skill_research_only"


def test_predictions_are_issued_and_fingerprinted_before_future_outcome(tmp_path):
    store = SQLiteHotStore(tmp_path / "forward-regimes.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    now = datetime.now(UTC)
    seed(market, now=now)
    try:
        assert monitor.run_once(now=now)[0] == 3
        with store.connect() as conn:
            tags = conn.execute(
                """SELECT * FROM perp_forward_regimes ORDER BY horizon_minutes"""
            ).fetchall()
        assert len(tags) == 3
        assert all(tag["method_version"] == VERSION for tag in tags)
        assert all(tag["regime"] in ("quiet", "normal", "turbulent") for tag in tags)
        assert all(len(tag["feature_sha256"]) == 64 for tag in tags)
        assert all(len(tag["weights_sha256"]) == 64 for tag in tags)
        assert all(
            datetime.fromisoformat(tag["issued_at"])
            < datetime.fromisoformat(tag["reference_opened_at"]) + timedelta(minutes=20)
            for tag in tags
        )
        assert monitor.run_once(now=now) == (0, 0)
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_regimes"
            ).fetchone()["n"] == 3
        recent = monitor.recent_issued(asset="BTC")
        assert len(recent) == 3
        assert all(row["settled_at"] is None for row in recent)
        summary = monitor.regime_summary(asset="BTC", horizon_minutes=5, now=now)
        assert summary["unique_target_windows"] == 0
        assert summary["status"] == "insufficient_distinct_market_conditions"
    finally:
        store.close()
