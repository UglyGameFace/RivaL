"""Up/Down contract chance from the live exchange price: the binary-option formula.

A 5/15/60-minute Polymarket Up/Down contract pays if the coin's price at the
close is at or above its price at the open. Treating the price as a random walk
with no drift over minutes, the chance of UP is

    P(UP) = N( ln(S / S0) / (sigma * sqrt(T)) )

S = price now, S0 = price at the open, sigma = per-second volatility measured
from the last hour of 1-minute candles, T = seconds left, N = the standard
normal distribution. This module is pure arithmetic; whether it beats
Polymarket's price is decided only by the locked scorecard in
``polymarket_formula_test``.
"""
from __future__ import annotations

import math
from itertools import pairwise
from statistics import pstdev


def per_second_sigma(closes: list[float]) -> float | None:
    """Volatility per second from consecutive 1-minute closes (oldest first)."""
    prices = [p for p in closes if isinstance(p, (int, float)) and math.isfinite(p) and p > 0]
    if len(prices) < 20:
        return None
    returns = [math.log(b / a) for a, b in pairwise(prices)]
    sigma_minute = pstdev(returns)
    if not math.isfinite(sigma_minute) or sigma_minute <= 0:
        return None
    return sigma_minute / math.sqrt(60)


def chance_up(price: float, open_price: float, sigma_per_second: float,
              seconds_left: float) -> float | None:
    if not (price > 0 and open_price > 0 and sigma_per_second > 0 and seconds_left > 0):
        return None
    z = math.log(price / open_price) / (sigma_per_second * math.sqrt(seconds_left))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))
