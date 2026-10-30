"""Conservative, window-deduplicated calibration diagnostics for real forecasts.

Each target in one asset+horizon is a single outcome even if ten exchanges
reported it. Day blocks are a heuristic for correlated outcomes, not an
exact frequentist confidence interval, promise of profit, or betting oracle.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

MIN_UNIQUE_WINDOWS = 150
MIN_UTC_DAYS = 21
MAX_ECE = 0.10
MIN_PAIRED_BRIER_GAIN = 0.005


def unique_window_calibration(rows: list[dict]) -> dict:
    """Only score immutable, prospectively issued and genuinely settled rows.

    Fails closed on duplicated target windows, invalid clocks, nonfinite
    probabilities, labels or inconsistent stored Brier losses.
    """
    windows: dict[str, tuple[float, float, int]] = {}
    days: dict[str, list[float]] = {}
    bins = [{"n": 0, "sum_p": 0.0, "wins": 0} for _ in range(10)]
    for row in rows:
        try:
            target = datetime.fromisoformat(str(row["target_opened_at"]))
            issued = datetime.fromisoformat(str(row["issued_at"]))
            settled = datetime.fromisoformat(str(row["settled_at"]))
            p = float(row["probability_up"])
            prior = float(row["reference_probability_up"])
            outcome = int(row["outcome_up"])
        except (TypeError, ValueError, OverflowError, KeyError) as exc:
            raise ValueError("invalid forward calibration evidence") from exc
        if (
            target.utcoffset() is None or issued.utcoffset() is None
            or settled.utcoffset() is None or outcome not in (0, 1)
            or not (0 < p < 1 and 0 < prior < 1)
            or not (math.isfinite(p) and math.isfinite(prior))
            or issued >= settled or issued >= target
            or settled.astimezone(UTC) <= target.astimezone(UTC)
            or target.astimezone(UTC).isoformat() != str(row["target_opened_at"])
        ):
            raise ValueError("non-causal or nonfinite forward calibration evidence")
        key = target.isoformat()
        if key in windows:
            raise ValueError("correlated duplicate target counted twice")
        windows[key] = (p, prior, outcome)
        day = target.date().isoformat()
        days.setdefault(day, []).append((prior-outcome)**2 - (p-outcome)**2)
        bucket = min(9, int(p * 10))
        bins[bucket]["n"] += 1
        bins[bucket]["sum_p"] += p
        bins[bucket]["wins"] += outcome

    n = len(windows)
    d = len(days)
    if not n:
        return {
            "status": "insufficient", "unique_windows": 0, "utc_days": 0,
            "brier": None, "incumbent_brier": None, "ece": None,
            "day_block_lower": None, "positive_days": 0,
            "reason": "No genuine settled forward outcomes",
        }
    brier = sum((p-outcome)**2 for p, _, outcome in windows.values())/n
    prior_brier = sum((q-outcome)**2 for _, q, outcome in windows.values())/n
    ece = sum(
        (b["n"]/n)*abs(b["sum_p"]/b["n"]-b["wins"]/b["n"])
        for b in bins if b["n"]
    )
    daily = [sum(xs)/len(xs) for xs in days.values()]
    mean = sum(daily)/d
    variance = sum((x-mean)**2 for x in daily)/(d-1) if d > 1 else 0.
    lower = mean - 2.58*math.sqrt(variance/d) if d > 1 else None
    positive = sum(x > 0 for x in daily)
    if n < MIN_UNIQUE_WINDOWS or d < MIN_UTC_DAYS:
        status, reason = "insufficient", "Need 150 unique UTC outcomes across 21 days"
    elif ece > MAX_ECE:
        status, reason = "not_calibrated", "Observed probabilities fail calibration screen"
    elif prior_brier - brier < MIN_PAIRED_BRIER_GAIN:
        status, reason = "not_better", "Model does not beat the same-window baseline"
    elif lower is None or lower <= 0 or positive < math.ceil(.8*d):
        status, reason = "unstable", "Paired day-block improvement is not stable"
    else:
        status, reason = "research_screen_passed", (
            "Calibration and matched forward scores passed conservative screens; "
            "not a betting-contract probability or real-money proof"
        )
    return {
        "status": status, "unique_windows": n, "utc_days": d,
        "brier": brier, "incumbent_brier": prior_brier, "ece": ece,
        "day_block_lower": lower, "positive_days": positive,
        "reason": reason,
    }
