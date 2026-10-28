"""Conservative, paired, day-blocked evaluation of forward shadow models.

Each UTC target is ONE correlated event even when multiple exchanges observed
it. Model and baseline are compared on the SAME target, using outcomes
recorded prospectively. This is research evidence, not a trading approval or
an independence/normality guarantee.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

MIN_TARGETS = 100
MIN_DAYS = 7
MIN_GAIN = 0.005
DAY_MARGIN = 2.58  # Conservative screening margin, not an exact p-value.


@dataclass(frozen=True)
class GovernanceEvidence:
    targets: int
    days: int
    model_brier: float | None
    baseline_brier: float | None
    paired_gain: float | None
    lower_day_bound: float | None
    positive_days: int
    status: str
    reason: str


def score_paired_forward(rows: list[tuple[str, float, float]]) -> GovernanceEvidence:
    """Use target-level paired Brier differences with a daily stress gate.

    The 2.58 daily-SE buffer is a *heuristic conservative screen* because
    seven daily blocks still have possible cross-day serial correlation.
    Never call it a formal 95% confidence interval or proof of profit.
    """
    by_target: dict[str, list[tuple[float, float]]] = {}
    for target, model, baseline in rows:
        try:
            when = datetime.fromisoformat(target)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid forward target timestamp") from exc
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("forward target requires UTC offset")
        if when.astimezone(UTC).isoformat() != target:
            raise ValueError("forward target must be canonical UTC")
        if not (math.isfinite(model) and math.isfinite(baseline)
                and 0 <= model <= 1 and 0 <= baseline <= 1):
            raise ValueError("invalid prospective Brier loss")
        by_target.setdefault(target, []).append((model, baseline))

    paired: list[tuple[str, float, float]] = []
    per_day: dict[str, list[float]] = {}
    for target, group in sorted(by_target.items()):
        model = sum(a for a, _ in group) / len(group)
        baseline = sum(b for _, b in group) / len(group)
        paired.append((target, model, baseline))
        day = datetime.fromisoformat(target).date().isoformat()
        per_day.setdefault(day, []).append(baseline - model)

    n = len(paired)
    days = len(per_day)
    if not n:
        return GovernanceEvidence(0, 0, None, None, None, None, 0,
                                  "insufficient", "No settled forward targets")
    m = sum(x[1] for x in paired) / n
    b = sum(x[2] for x in paired) / n
    delta = b - m
    daily = [sum(values) / len(values) for values in per_day.values()]
    mean_daily = sum(daily) / days
    variance = (sum((x - mean_daily) ** 2 for x in daily) / (days - 1)
                if days > 1 else 0.0)
    bound = mean_daily - DAY_MARGIN * math.sqrt(variance / days)
    winning = sum(x > 0 for x in daily)
    if n < MIN_TARGETS or days < MIN_DAYS:
        status, reason = "insufficient", "Need 100 unique UTC targets across 7 days"
    elif delta <= MIN_GAIN:
        status, reason = "rejected_shadow", "Paired Brier gain below minimum"
    elif winning < math.ceil(0.8 * days):
        status, reason = "rejected_shadow", "Paired benefit inconsistent across days"
    elif bound <= 0:
        status, reason = "rejected_shadow", "Day-block stress bound not positive"
    else:
        status, reason = (
            "qualified_shadow",
            "Passed paired Brier, daily consistency and stress screens; shadow only",
        )
    return GovernanceEvidence(
        n, days, m, b, delta, bound, winning, status, reason,
    )
