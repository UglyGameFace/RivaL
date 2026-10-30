"""Boundary math must fail closed, including overflow and negative-priced slips."""
from __future__ import annotations

from dataclasses import replace

import pytest

from parlay_bot.parlays.builder import _PricedCandidate
from parlay_bot.parlays.math import (
    combined_decimal_odds,
    expected_return_edge,
    implied_probability,
    independent_joint_probability,
    no_vig_probabilities,
)
from parlay_bot.parlays.models import ParlayBuildError


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True])
def test_invalid_odds_and_probability_are_rejected(bad):
    with pytest.raises((TypeError, ValueError)):
        implied_probability(bad)
    with pytest.raises((TypeError, ValueError)):
        no_vig_probabilities([2, bad])
    with pytest.raises((TypeError, ValueError)):
        combined_decimal_odds([2, bad])
    with pytest.raises((TypeError, ValueError)):
        expected_return_edge(fair_probability=0.5, decimal_odds=bad)
    with pytest.raises((TypeError, ValueError)):
        expected_return_edge(fair_probability=bad, decimal_odds=2)
    with pytest.raises((TypeError, ValueError)):
        independent_joint_probability([0.5, bad])


def test_finite_individual_odds_cannot_overflow_combined_price():
    with pytest.raises(ValueError, match="overflow"):
        combined_decimal_odds([1e308, 1e308])


def test_negative_combined_indicative_edge_cannot_be_auto_suggested(tmp_path):
    from tests.test_parlays import build_engine
    engine, store = build_engine(tmp_path)
    try:
        # Simulate candidates already selected upstream, so the final
        # aggregate guard catches an inconsistent upstream indicative score.
        candidate = _PricedCandidate(
            bookmaker="fanduel", fixture_id="game-0", event_name="Game",
            market_id="1", market_name="Winner", outcome_id="2",
            outcome_name="Home", player_id="0", player_name=None,
            line_value=None, line_group_value=None, deeplink=None,
            price_decimal=2.0, price_american="+100",
            fair_probability=0.49, price_edge=0.01,
            reference_books=2, score=1.0,
        )
        engine._candidates = lambda **kwargs: [
            replace(candidate, fixture_id=f"game-{i}") for i in range(3)
        ]
        with pytest.raises(ParlayBuildError, match="independent fixtures"):
            engine.build_for_state(state_code="CT", leg_count=3)
        assert engine.last_diagnostics.get("negative_combined_edge") == 1
    finally:
        store.close()
