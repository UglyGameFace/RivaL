"""Regime gate for the regime_v1 challenger (spec rival-regime-gated-spec-20261107).

D1: the gate is the Kaufman efficiency ratio over the last 20 closed
5-minute bars, ER > 0.30, frozen and predeclared. The gate reads closes
only: no new data, no new feature, no fit. It never raises: anything it
cannot evaluate is an abstention, never a failed incumbent cycle.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from parlay_bot.perpetuals.fusion import utc

# Frozen by the spec (D1). Changing this number is a new challenger family,
# not a tune: it changes which targets the family issues on, so its
# promotion history does not transfer.
ER_WINDOW = 20  # moves (21 closes)
ER_THRESHOLD = 0.30


def efficiency_ratio(closes: Sequence[float]) -> float | None:
    """Kaufman efficiency ratio over the last 20 moves.

    |close[-1] - close[-21]| / sum(|close[i+1] - close[i]|), 0..1.
    1.0 is a straight line, 0.0 is pure noise. Returns None when the
    input cannot be evaluated (too few closes, a non-positive or
    non-finite close); a flat market evaluates to 0.0, not None.
    """
    if closes is None or len(closes) < ER_WINDOW + 1:
        return None
    window = [float(c) for c in closes[-(ER_WINDOW + 1):]]
    if any(not math.isfinite(c) or c <= 0 for c in window):
        return None
    moves = [abs(b - a) for a, b in itertools.pairwise(window)]
    denominator = sum(moves)
    if denominator <= 0:
        return 0.0
    return abs(window[-1] - window[0]) / denominator


def trending_gate(candles: Sequence) -> bool:
    """True when the last 20 closed 5-minute bars are trending (ER > 0.30).

    The gate reads closes only and uses closed bars only: it looks at the
    last 21 bars of the sequence the caller passes, which must be closed
    5-minute candles. Anything else -- too few bars, a gap in the 300s
    spacing, a venue/contract/quote change inside the window, a
    non-positive or non-finite close -- abstains (False). Never raises.
    """
    try:
        if candles is None or len(candles) < ER_WINDOW + 1:
            return False
        window = candles[-(ER_WINDOW + 1):]
        for left, right in itertools.pairwise(window):
            if (
                left.venue != right.venue
                or left.contract != right.contract
                or left.quote != right.quote
            ):
                return False
            if (
                utc(right.opened_at) - utc(left.opened_at)
            ).total_seconds() != 300:
                return False
        ratio = efficiency_ratio([c.close_price for c in window])
        return ratio is not None and ratio > ER_THRESHOLD
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
