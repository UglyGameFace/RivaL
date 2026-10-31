"""Explicit, calibrated-later research baselines for binary threshold contracts.

Lognormal digital model with a source-estimated remaining-horizon volatility.
This is a benchmark hypothesis, NOT a claim that short-window TWAP returns
follow independent Gaussian increments, or a profitable trading signal.
"""
from __future__ import annotations

import math


def threshold_up_probability(
    *, current_oracle_price: float, price_to_beat: float,
    remaining_volatility_pct: float, drift_pct: float = 0.0,
) -> float:
    """P(terminal reference >= strike) under a *hypothetical* lognormal law.

    The caller is responsible for source-matched oracle data and an unbiased
    estimate of uncertainty through settlement, including any TWAP smoothing.
    No market is promoted based on the mathematical assumption alone.
    """
    values = (
        current_oracle_price, price_to_beat,
        remaining_volatility_pct, drift_pct,
    )
    if not all(math.isfinite(v) for v in values):
        raise ValueError("binary contract inputs must be finite")
    if (
        current_oracle_price <= 0 or price_to_beat <= 0
        or not 0 < remaining_volatility_pct <= 20
        or not -20 <= drift_pct <= 20
    ):
        raise ValueError("invalid reference, strike, volatility or drift")
    # Relative distance is symmetric in multiplicative price space.
    sigma = remaining_volatility_pct/100
    z = (math.log(current_oracle_price/price_to_beat)
         + (drift_pct/100) - .5*sigma*sigma)/sigma
    return .5*math.erfc(-z/math.sqrt(2.))


def candidate_edge_after_cost(
    *, predicted_up: float, yes_ask: float, no_ask: float,
    fee_per_share: float | None, slippage_per_share: float | None,
) -> dict:
    """Explicit signed expected payout less executable ask and all costs.

    If provider fees or executable slippage are unknown, refuse EV claims.
    A positive mathematical EV based on a calibrated forecast is still not
    realized performance, and this function cannot place an order.
    """
    probabilities = (predicted_up, yes_ask, no_ask)
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in probabilities):
        raise ValueError("invalid contract prices/probability")
    if fee_per_share is None or slippage_per_share is None:
        return {
            "status": "unknown_cost", "yes_ev": None, "no_ev": None,
            "verified_edge": False,
        }
    if not all(
        math.isfinite(v) and 0 <= v <= 1
        for v in (fee_per_share, slippage_per_share)
    ):
        raise ValueError("invalid contract costs")
    cost = fee_per_share + slippage_per_share
    return {
        "status": "simulated_only",
        "yes_ev": predicted_up - yes_ask - cost,
        "no_ev": 1-predicted_up - no_ask - cost,
        "verified_edge": False,
    }
