"""Reviewed digital-option research baselines; see POLYMARKET_MODEL_CONTEST.md.

Zero arithmetic drift GBM uses d2. EWMA and a separate Merton jump mixture
are shadow hypotheses, not calibrated event probabilities. A trailing TWAP
is an Asian payoff: its labeled moment approximation is not an exact digital.
"""
from __future__ import annotations

import math
from itertools import pairwise
from statistics import median

EWMA_DECAY = 0.94  # fixed research setting, not a calibrated crypto optimum


def _returns(closes: list[float]) -> list[float] | None:
    if len(closes) < 20 or any(
        not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0 for p in closes
    ):
        return None  # dropping bad prices would bridge an unobserved interval
    return [math.log(b / a) for a, b in pairwise(closes)]


def per_second_sigma(closes: list[float]) -> float | None:
    """Zero-mean EWMA of consecutive CLOSED one-minute log returns."""
    returns = _returns(closes)
    if returns is None:
        return None
    variance = sum(r*r for r in returns[:19]) / 19
    for value in returns[19:]:
        variance = EWMA_DECAY * variance + (1 - EWMA_DECAY) * value * value
    return math.sqrt(variance / 60) if math.isfinite(variance) and variance > 0 else None


def _normal(z: float) -> float:
    return .5 * math.erfc(-z / math.sqrt(2))


def chance_up(price: float, open_price: float, sigma_per_second: float,
              seconds_left: float, *, twap_seconds: int = 0) -> float | None:
    values = (price, open_price, sigma_per_second, seconds_left)
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return None
    if twap_seconds < 0 or twap_seconds >= seconds_left:
        return None  # partially accrued TWAP requires its observed path
    variance = sigma_per_second**2 * seconds_left
    if not math.isfinite(variance) or variance <= 0:
        return None
    if twap_seconds:
        # E[A^2]/S^2 = exp(v*(T-w))*2*(exp(v*w)-1-v*w)/(v*w)^2.
        # Lognormal moment match, stable even for tiny v*w.
        x = sigma_per_second**2 * twap_seconds
        if x > 700:
            return None
        ratio = (1 + x/3 + x*x/12 + x**3/60 if x < 1e-3
                 else 2 * (math.expm1(x) - x) / (x*x))
        variance = sigma_per_second**2 * (seconds_left - twap_seconds) + math.log(ratio)
    return _normal((math.log(price / open_price) - .5 * variance) / math.sqrt(variance))


def jump_parameters(closes: list[float]) -> dict | None:
    """Fixed robust threshold/moment estimator from past returns.

    This shadow estimator is not Merton maximum-likelihood calibration.
    Two observed tail returns are required; no invented jump sizes.
    """
    returns = _returns(closes)
    if returns is None or len(returns) < 119:
        return None
    center = median(returns)
    scale = 1.4826 * median(abs(r - center) for r in returns)
    if scale <= 0:
        return None
    jumps = [r for r in returns if abs(r - center) > 3 * scale]
    calm = [r for r in returns if abs(r - center) <= 3 * scale]
    if len(jumps) < 2 or len(calm) < 20:
        return None
    mean = sum(jumps) / len(jumps)
    variance = sum((r - mean)**2 for r in jumps) / len(jumps)
    return {"sigma": math.sqrt(sum(r*r for r in calm) / len(calm) / 60),
            "intensity": len(jumps) / (len(returns) * 60),
            "mean": mean, "variance": variance}


def jump_chance(price: float, strike: float, seconds_left: float,
                parameters: dict, *, twap_seconds: int = 0) -> float | None:
    """Merton Poisson mixture; TWAP uses an explicit effective-time approximation.

    For TWAP the first two cumulants use averaging weights. This separate
    shadow hypothesis is never described as an exact Asian-option price.
    """
    sigma, intensity = parameters["sigma"], parameters["intensity"]
    mean, variance = parameters["mean"], parameters["variance"]
    if (not all(math.isfinite(x) for x in (price, strike, seconds_left, sigma,
                                          intensity, mean, variance))
            or min(price, strike, seconds_left, sigma) <= 0
            or min(intensity, variance) < 0 or not 0 <= twap_seconds < seconds_left):
        return None
    duration = seconds_left - 2 * twap_seconds / 3
    lam = intensity * duration
    if lam > 32 or abs(mean) + variance > 1:
        return None
    drift = (-.5 * sigma*sigma - intensity * math.expm1(mean + variance/2)) * duration
    weight = math.exp(-lam)
    total = mass = 0.0
    for count in range(128):
        v = sigma*sigma * duration + count * variance
        z = (math.log(price / strike) + drift + count*mean) / math.sqrt(v)
        total += weight * _normal(z)
        mass += weight
        if 1 - mass < 1e-12:
            return min(1., max(0., total))
        weight *= lam / (count + 1)
    return None


def microprice(bid: float, ask: float, bid_size: float, ask_size: float) -> float | None:
    """Queue-weighted price proxy, not event-based order FLOW imbalance.

    No invented pressure-to-return coefficient. Its incremental value must
    pass the same prospective contest as the unadjusted price baseline.
    """
    if (not all(math.isfinite(x) and x > 0 for x in (bid, ask, bid_size, ask_size))
            or bid >= ask):
        return None
    return (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
