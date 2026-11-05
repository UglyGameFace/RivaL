"""Venue-prior challengers: a pooled model's call re-centred on this venue's up-rate.

The flow and six-year pooled models learn one up-rate across venues (the
six-year model from Binance, where a flat 5-minute close is rare). Venues
differ a lot: in the week to Nov 5, 2026, dYdX closed flat on 27-28% of BTC/ETH
5-minute targets, 55% of SOL and 97% of XRP, and a flat close is graded as
NOT UP. The incumbent is fitted per venue and its up-rate baseline is too, so a
venue-blind model loses there even when its signal is fine.

A venue-prior variant keeps the model's evidence (its log-odds relative to the
up-rate it was trained on) and moves the prior to this stream's own up-rate,
known at issuance from labels already matured:

    logit(p_venue) = logit(p_model) - logit(trained_up) + logit(stream_up)

It is a NEW shadow family beside the original, graded on the same outcomes
under the same watchdog and promotion gates; nothing is replaced.
"""
from __future__ import annotations

import math

SUFFIX = "_venue"
FLOOR, CEIL = 0.02, 0.98
# The six-year model trains on Binance 5-minute bars, where flat closes are
# rare, so its learned up-rate is close to one half. Not stored in the model
# file, so taken as 0.5 (the correction then equals the stream's own prior).
POOLED_TRAINED_UP = 0.5


def _logit(p: float) -> float:
    p = min(CEIL, max(FLOOR, p))
    return math.log(p / (1 - p))


def venue_prior_probability(model_up: float, *, trained_up: float, stream_up: float) -> float:
    if not all(math.isfinite(v) and 0 < v < 1 for v in (model_up, trained_up, stream_up)):
        raise ValueError("venue prior needs probabilities strictly between 0 and 1")
    value = _logit(model_up) - _logit(trained_up) + _logit(stream_up)
    return min(CEIL, max(FLOOR, 1 / (1 + math.exp(-max(-30.0, min(30.0, value))))))


def venue_prior_calls(calls, *, stream_up: float) -> list[dict]:
    """One '<family>_venue' call per pooled or flow call; others are skipped.

    ``trained_up`` comes from the call when the model reports it (flow
    models), else POOLED_TRAINED_UP for the six-year pooled models.
    """
    out = []
    for call in calls or ():
        try:
            family = str(call["family"])
            if not family.startswith(("pooled_", "flow_")) or family.endswith(SUFFIX):
                continue
            name = family + SUFFIX
            if len(name) > 64:
                continue
            trained = float(call.get("trained_up") or POOLED_TRAINED_UP)
            out.append({
                "family": name,
                "probability": venue_prior_probability(
                    float(call["probability"]), trained_up=trained, stream_up=stream_up),
                "model_sha256": call["model_sha256"],
                "training_rows": int(call.get("training_rows") or 0),
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return out
