from __future__ import annotations

import pytest

from parlay_bot.providers.budget_policy import ProviderBudget, implied_probability


def test_unknown_provider_allowance_fails_closed():
    budget = ProviderBudget(configured_limit=1000, reserve=20, verified_limit=None, verified_used=None)
    assert budget.available == 0
    assert not budget.permits(1)


def test_paid_upgrade_never_overrides_lower_provider_ceiling():
    budget = ProviderBudget(configured_limit=100000, reserve=20, verified_limit=500, verified_used=470)
    assert budget.effective_limit == 500
    assert budget.available == 10
    assert budget.permits(10)
    assert not budget.permits(11)


def test_owner_lower_limit_and_local_reservations():
    budget = ProviderBudget(configured_limit=100, reserve=10, verified_limit=1000, verified_used=40, locally_reserved=20)
    assert budget.available == 30


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_odds_rejected(value):
    with pytest.raises(ValueError):
        implied_probability(value)


def test_market_probability_not_model_prediction():
    assert implied_probability(2.5) == pytest.approx(0.4)
