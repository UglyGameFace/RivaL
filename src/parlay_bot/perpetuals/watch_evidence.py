"""Conservative, venue/model-specific proof gate for directional research WATCH.

The 50-forecast count is NOT a statistical significance test. Forecasts
within one UTC day share market conditions and overlapping targets, so a
single lucky session must not qualify the next live signal. This day-block
guard is intentionally more conservative than a raw mean-Brier comparison.
It is not a proof of independence, profitability, or executable edge.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping

MIN_WATCH_SETTLED = 50
MIN_WATCH_DAYS = 5
MIN_DAILY_SETTLED = 5
MIN_MEAN_BRIER_GAIN = 0.01
# Two-sided 95% Student-t critical values for 4..7 degrees of freedom.
_T_95 = {5: 2.776, 6: 2.571, 7: 2.447}


def day_block_watch_evidence(rows: Iterable[Mapping]) -> dict:
    """Inspect daily paired, prospective Brier scores without claiming trade edge.

    Each row has n, model_sum, baseline_sum, invalid_rows from one UTC
    issuance day. Invalid/missing scores fail closed instead of silently
    improving the average through SQL NULL skipping.
    """
    days = []
    total = 0
    total_gain = 0.0
    invalid = False
    for row in rows:
        count = int(row["n"])
        model = row["model_sum"]
        baseline = row["baseline_sum"]
        if count <= 0 or int(row["invalid_rows"] or 0) != 0:
            invalid = True
            continue
        if model is None or baseline is None:
            invalid = True
            continue
        model, baseline = float(model), float(baseline)
        if not math.isfinite(model) or not math.isfinite(baseline):
            invalid = True
            continue
        total += count
        total_gain += baseline - model
        if count >= MIN_DAILY_SETTLED:
            days.append((baseline - model) / count)
    usable_days = len(days)
    result = {
        "watch_evidence_passes": False,
        "watch_days": usable_days,
        "watch_brier_gain_lower_bound": None,
        "watch_reason": "insufficient_settled",
    }
    if invalid:
        result["watch_reason"] = "invalid_scores"
        return result
    if total < MIN_WATCH_SETTLED:
        return result
    if usable_days < MIN_WATCH_DAYS:
        result["watch_reason"] = "insufficient_days"
        return result
    average_day_gain = statistics.mean(days)
    t_critical = _T_95.get(usable_days, 2.365)  # conservative for 8+ days
    lower = average_day_gain - (
        t_critical * statistics.stdev(days) / math.sqrt(usable_days)
    )
    result["watch_brier_gain_lower_bound"] = lower
    if total_gain / total < MIN_MEAN_BRIER_GAIN or lower <= 0:
        result["watch_reason"] = "uncertain_gain"
        return result
    result["watch_evidence_passes"] = True
    result["watch_reason"] = "day_block_pass"
    return result
