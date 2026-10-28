"""Forward-only probability calibration, one outcome per UTC target.

This is a reliability measurement, not in-sample fitting, a claim of a
profitable edge, or a post-hoc change to stored prospective probabilities.
Any exchange disagreement is counted and withheld from binary calibration.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

MIN_TARGETS = 100
MIN_DAYS = 7
MIN_OCCUPIED_BIN = 15
MIN_DENSE_BINS = 2
MAX_PRELIMINARY_ECE = 0.08


def _date(value: object) -> datetime:
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid forward target time") from exc
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("forward outcomes need aware UTC time")
    if t.astimezone(UTC).isoformat() != str(value):
        raise ValueError("forward target must be canonical UTC")
    return t


def forward_reliability(rows: list[dict[str, Any]]) -> dict:
    """Report calibrated frequency by probability bin, never fit on outcomes.

    This function is only fed genuine *settled* future forecast rows, already
    filtered to the exact prospective math model version in the caller.
    It refuses non-binary/discordant venue labels rather than silently
    averaging different futures into a fractional 'win' probability.
    """
    seen: dict[str, tuple[float, int]] = {}
    disputed = 0
    for row in rows:
        timestamp = str(row["target_opened_at"])
        _date(timestamp)
        probability = float(row["probability"])
        outcome = float(row["up_rate"])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("invalid forward probability")
        if not math.isfinite(outcome) or not 0 <= outcome <= 1:
            raise ValueError("invalid forward target outcome")
        if outcome not in (0, 1):
            disputed += 1
            continue
        if timestamp in seen:
            old_p, old_y = seen[timestamp]
            if old_y != int(outcome):
                disputed += 1
                del seen[timestamp]
                continue
            # Repeated source-specific results must not multiply targets.
            seen[timestamp] = ((old_p + probability) / 2, old_y)
        else:
            seen[timestamp] = (probability, int(outcome))
    buckets: dict[int, list[tuple[float, int]]] = {}
    for probability, label in seen.values():
        ix = min(9, int(probability * 10))
        buckets.setdefault(ix, []).append((probability, label))
    total = len(seen)
    days = len({_date(x).date().isoformat() for x in seen})
    detail = []
    for index, values in sorted(buckets.items()):
        count = len(values)
        pred = sum(x for x, _ in values) / count
        observed = sum(y for _, y in values) / count
        # Wilson score 95% interval is descriptive, not an independence
        # guarantee for serially correlated 5m/15m time series.
        z = 1.96
        center = (observed + z * z / (2 * count)) / (1 + z * z / count)
        width = (
            z * math.sqrt(
                observed * (1 - observed) / count
                + z * z / (4 * count * count)
            ) / (1 + z * z / count)
        )
        detail.append({
            "bin": index, "n": count,
            "average_probability": round(pred, 5),
            "observed_up_rate": round(observed, 5),
            "gap": round(abs(pred - observed), 5),
            "wilson_lower": round(max(0, center - width), 5),
            "wilson_upper": round(min(1, center + width), 5),
        })
    ece = (
        sum(group["n"] * group["gap"] for group in detail) / total
        if total else None
    )
    dense = sum(group["n"] >= MIN_OCCUPIED_BIN for group in detail)
    if total < MIN_TARGETS or days < MIN_DAYS:
        status = "insufficient_independent_forward_history"
    elif dense < MIN_DENSE_BINS:
        status = "narrow_probability_coverage"
    elif ece is None or ece > MAX_PRELIMINARY_ECE:
        status = "calibration_gap_too_high"
    else:
        status = "preliminary_reliability_screen_only"
    return {
        "unique_targets": total,
        "days": days,
        "disputed_targets": disputed,
        "ece": round(ece, 5) if ece is not None else None,
        "occupied_bins": len(detail),
        "dense_bins": dense,
        "bins": detail,
        "status": status,
        "required_targets": MIN_TARGETS,
        "required_days": MIN_DAYS,
        "recalibration_trained": False,
        "causal_forecast_only": True,
        "approved_for_trading": False,
        "independence_proven": False,
    }
