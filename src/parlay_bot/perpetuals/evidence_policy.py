"""Historical observation guard for uncalibrated local LLM research.

This is the single source of truth for both the main bot and the standalone
Discloud gateway; CI copies this exact module into the isolated ZIP.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

WINDOWS = {5: ("5m", 1), 15: ("15m", 3), 60: ("1h", 12), 240: ("4h", 48)}
QUOTES = frozenset({"USD", "USDT", "USDC"})


def _closed_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.utcoffset() is None:
        return None
    return stamp.astimezone(UTC)


def classify(evidence: object, *, asset: str, now: datetime | None = None) -> dict:
    """Never infer future performance from historical candles or input claims."""
    summary = {"observed_trend": None, "grounding_status": "insufficient_evidence",
               "usable_venues": 0}
    if not isinstance(evidence, dict):
        return summary
    if evidence.get("analysis") != "unvalidated_research" or evidence.get("asset") != asset:
        return summary
    horizon = evidence.get("horizon_minutes")
    if type(horizon) is not int or horizon not in WINDOWS:
        return summary
    sources = evidence.get("sources")
    if not isinstance(sources, list):
        return summary
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None:
        return summary
    clock = clock.astimezone(UTC)
    label, bars = WINDOWS[horizon]
    observed = []
    names = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        venue = source.get("venue")
        if not isinstance(venue, str) or not venue.strip():
            continue
        if venue in names:
            # Repeated venue entries cannot count as independent confirmations.
            return summary
        names.add(venue)
        if source.get("quote") not in QUOTES:
            continue
        if not isinstance(source.get("contract"), str) or not source["contract"]:
            continue
        length = source.get("available_bars")
        if type(length) is not int or length < 13:
            continue
        stamp = _closed_at(source.get("last_candle_close"))
        if stamp is None or not timedelta(0) <= clock - stamp <= timedelta(minutes=7):
            continue
        windows = source.get("windows")
        window = windows.get(label) if isinstance(windows, dict) else None
        if not isinstance(window, dict):
            continue
        sample = window.get("sample_bars")
        change = window.get("return_pct")
        if type(sample) is not int or not bars <= sample <= length - 1:
            continue
        if type(change) not in (int, float) or not math.isfinite(change):
            continue
        observed.append((stamp, float(change)))
    summary["usable_venues"] = len(observed)
    if len(observed) < 3:
        return summary
    stamps = [item[0] for item in observed]
    if max(stamps) - min(stamps) > timedelta(minutes=5):
        return summary
    moves = [item[1] for item in observed]
    if all(r > 0 for r in moves):
        summary["observed_trend"] = "up"
    elif all(r < 0 for r in moves):
        summary["observed_trend"] = "down"
    summary["grounding_status"] = (
        "historical_observations_only" if summary["observed_trend"]
        else "conflicting_historical_observations"
    )
    return summary


def enforce(reply: dict, evidence: object, *, asset: str,
            now: datetime | None = None) -> dict:
    """Sanitize future-looking output, without changing RivaL's math model."""
    facts = classify(evidence, asset=asset, now=now)
    trend = facts["observed_trend"]
    count = facts["usable_venues"]
    horizon = evidence.get("horizon_minutes") if isinstance(evidence, dict) else None
    if trend:
        past_verb = "rose" if trend == "up" else "fell"
        thesis = (f"Past {horizon}m closed-candle returns {past_verb} across "
                  f"{count} independent recent venues. This describes past "
                  "movement, not a forecast of the next interval.")
    elif facts["grounding_status"] == "conflicting_historical_observations":
        thesis = (f"Past closed-candle returns disagree across {count} "
                  "independent recent venues. Future direction is unverified.")
    else:
        thesis = ("Insufficient fresh independent closed-candle evidence to "
                  "support a market direction.")
    risk = ("No verified future advantage, synchronized executable bid/ask "
            "quotes, or after-cost fills. Fees, funding and slippage are unverified.")
    return {
        **reply, "direction": "uncertain", "confidence": "low",
        "thesis": thesis, "risk": risk,
        **facts, "grounded_by": "deterministic_historical_evidence_policy",
        "model_direction_overridden": reply.get("direction") != "uncertain",
        "informational_only": True, "uncalibrated": True,
    }
