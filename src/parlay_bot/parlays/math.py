from __future__ import annotations

import math
from collections.abc import Iterable


def _finite(value: float, name: str) -> float:
    """Reject nonnumeric, boolean, NaN and infinite provider/model values."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def valid_decimal_odds(value: float) -> float:
    number = _finite(value, "decimal odds")
    if number <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return number


def valid_probability(value: float) -> float:
    number = _finite(value, "probability")
    if not 0 <= number <= 1:
        raise ValueError("probabilities must be between 0 and 1")
    return number


def implied_probability(decimal_odds: float) -> float:
    return 1.0 / valid_decimal_odds(decimal_odds)


def no_vig_probabilities(decimal_prices: Iterable[float]) -> list[float]:
    implied = [implied_probability(price) for price in decimal_prices]
    if len(implied) < 2:
        raise ValueError("no-vig normalization needs at least two outcomes")
    total = sum(implied)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("invalid implied probability total")
    return [value / total for value in implied]


def combined_decimal_odds(prices: Iterable[float]) -> float:
    values = [valid_decimal_odds(value) for value in prices]
    if not values:
        raise ValueError("at least one price is required")
    combined = math.prod(values)
    if not math.isfinite(combined):
        raise ValueError("combined decimal odds overflow")
    return combined


def independent_joint_probability(probabilities: Iterable[float]) -> float:
    values = [valid_probability(value) for value in probabilities]
    if not values:
        raise ValueError("at least one probability is required")
    result = math.prod(values)
    if not math.isfinite(result):
        raise ValueError("invalid combined probability")
    return result


def expected_return_edge(*, fair_probability: float, decimal_odds: float) -> float:
    probability = valid_probability(fair_probability)
    odds = valid_decimal_odds(decimal_odds)
    result = probability * odds - 1.0
    if not math.isfinite(result):
        raise ValueError("invalid expected return")
    return result
