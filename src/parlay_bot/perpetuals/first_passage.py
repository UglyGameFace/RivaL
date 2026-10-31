"""Causal first-passage math shared by historical and live research.

A 5-minute OHLC candle cannot establish whether TP or stop touched first if
both prices fall within that candle. Those examples MUST remain unresolved.
"""
from __future__ import annotations

import math


def trace_barriers(*, entry: float, sigma_horizon: float, side: str,
                   future: list[dict]) -> tuple[int, int] | None:
    """TP1/TP2 reached before a fixed stop, or None for ambiguous OHLC."""
    if side not in ("long", "short") or not math.isfinite(entry) or entry <= 0:
        raise ValueError("invalid future path identity or entry")
    if not math.isfinite(sigma_horizon) or not 0 < sigma_horizon <= .25:
        raise ValueError("invalid prior volatility")
    sign = 1 if side == "long" else -1
    tp1 = entry * math.exp(sign*sigma_horizon)
    tp2 = entry * math.exp(sign*2*sigma_horizon)
    stop = entry * math.exp(-sign*sigma_horizon)
    hit1 = False
    hit2 = False
    if not future:
        raise ValueError("no future bars")
    for bar in future:
        high, low = float(bar["high"]), float(bar["low"])
        if not (math.isfinite(high) and math.isfinite(low) and low > 0 and high >= low):
            raise ValueError("invalid future high/low")
        stopped = low <= stop if sign == 1 else high >= stop
        touched1 = high >= tp1 if sign == 1 else low <= tp1
        touched2 = high >= tp2 if sign == 1 else low <= tp2
        if stopped and ((touched1 and not hit1) or (touched2 and not hit2)):
            return None
        if stopped:
            return int(hit1), int(hit2)
        hit1 = hit1 or touched1
        hit2 = hit2 or touched2
        if hit2:
            return 1, 1
    return int(hit1), int(hit2)
