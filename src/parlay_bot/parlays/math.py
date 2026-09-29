from __future__ import annotations

import math
from collections.abc import Iterable


def implied_probability(decimal_odds: float) -> float:
    if decimal_odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return 1.0 / decimal_odds


def no_vig_probabilities(decimal_prices: Iterable[float]) -> list[float]:
    implied = [implied_probability(price) for price in decimal_prices]
    if len(implied) < 2:
        raise ValueError("no-vig normalization needs at least two outcomes")
    total = sum(implied)
    if total <= 0:
        raise ValueError("invalid implied probability total")
    return [value / total for value in implied]


def combined_decimal_odds(prices: Iterable[float]) -> float:
    values = list(prices)
    if not values:
        raise ValueError("at least one price is required")
    return math.prod(values)


def independent_joint_probability(probabilities: Iterable[float]) -> float:
    values = list(probabilities)
    if not values:
        raise ValueError("at least one probability is required")
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be between 0 and 1")
    return math.prod(values)


def expected_return_edge(*, fair_probability: float, decimal_odds: float) -> float:
    if fair_probability < 0 or fair_probability > 1:
        raise ValueError("fair_probability must be between 0 and 1")
    if decimal_odds <= 1:
        raise ValueError("decimal_odds must be greater than 1")
    return fair_probability * decimal_odds - 1.0
