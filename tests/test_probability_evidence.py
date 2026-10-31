"""The same proper scores must work for any exact binary contract venue."""
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.probability_evidence import binary_outcome_evidence


def test_one_binary_event_counts_once_and_is_prospectively_scored():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    items = [
        {
            "contract_id": f"KXBTC15M-{i}",
            "issued_at": (start+timedelta(days=i % 21,minutes=i)).isoformat(),
            "settlement_at": (
                start+timedelta(days=i % 21,minutes=i+20)
            ).isoformat(),
            "prediction": .95,
            "baseline": .5,
            "outcome": 1,
        }
        for i in range(150)
    ]
    results = binary_outcome_evidence(items)
    assert results["count"] == 150
    assert results["days"] == 21
    assert results["model_brier"] == pytest.approx(.0025)
    assert results["market_brier"] == pytest.approx(.25)
    assert results["model_ece"] == pytest.approx(.05)
    assert results["brier_gain"] > .2
    assert results["daily_brier_gain_lower_bound"] > 0
    assert results["positive_day_share"] == 1
    assert results["model_log_loss"] < results["market_log_loss"]


def test_prequential_binary_scores_fail_closed_for_leakage_and_duplicates():
    at = datetime(2026, 10, 1, tzinfo=UTC)
    item = {
        "contract_id": "KXBTC15M-ABC", "issued_at": at.isoformat(),
        "settlement_at": (at+timedelta(minutes=20)).isoformat(),
        "prediction": .6, "baseline": .5, "outcome": 1,
    }
    empty = binary_outcome_evidence([])
    assert empty["count"] == 0
    assert empty["daily_brier_gain_lower_bound"] is None
    with pytest.raises(ValueError):
        binary_outcome_evidence([item, item])
    with pytest.raises(ValueError):
        binary_outcome_evidence([{**item, "settlement_at": at.isoformat()}])
    with pytest.raises(ValueError):
        binary_outcome_evidence([{**item, "prediction": float("nan")}])
    with pytest.raises(ValueError):
        binary_outcome_evidence([{**item, "outcome": 2}])
    with pytest.raises(ValueError):
        binary_outcome_evidence([{**item, "issued_at": "2026-10-01T00:00:00"}])
