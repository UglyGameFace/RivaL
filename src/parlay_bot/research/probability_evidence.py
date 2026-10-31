"""Conservative prequential probability scoring for binary event markets.

One immutable forecast per exact contract. These statistics show agreement
with settled outcomes, never executable profit or permission to promote.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime


def _utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prediction/outcome timestamps must have UTC offsets")
    return value.astimezone(UTC)


def _ece(samples: list[tuple[float, int]], bins: int = 10) -> float | None:
    if not samples:
        return None
    groups: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for probability, outcome in samples:
        groups[min(bins - 1, int(probability * bins))].append(
            (probability, outcome)
        )
    return sum(
        len(group)/len(samples)
        * abs(sum(p for p, _ in group)/len(group)
              - sum(y for _, y in group)/len(group))
        for group in groups if group
    )


def binary_outcome_evidence(rows: list[dict]) -> dict:
    """Score ONLY paired predictions made before their matching outcomes.

    The improvement confidence bound uses daily paired averages rather than
    treating many simultaneous contracts as independent market days.
    It remains a research diagnostic, not a trading confidence interval.
    """
    seen = set()
    paired: list[tuple[float, float, int, str]] = []
    for row in rows:
        contract = row["contract_id"]
        issued = _utc(row["issued_at"])
        settled = _utc(row["settlement_at"])
        p = float(row["prediction"])
        baseline = float(row["baseline"])
        label = row["outcome"]
        if (
            not isinstance(contract, str) or not contract
            or contract in seen or settled <= issued
            or not all(math.isfinite(v) and 0 <= v <= 1 for v in (p, baseline))
            or label not in (0, 1)
        ):
            raise ValueError("invalid/duplicate/retrospective binary outcome evidence")
        seen.add(contract)
        paired.append((p, baseline, label, issued.date().isoformat()))
    n = len(paired)
    if not n:
        return {
            "count": 0, "days": 0, "model_brier": None,
            "market_brier": None, "model_log_loss": None,
            "market_log_loss": None, "model_ece": None,
            "market_ece": None, "brier_gain": None,
            "daily_brier_gain_lower_bound": None, "positive_day_share": None,
        }

    def loss(p: float, label: int) -> float:
        clipped = min(1 - 1e-12, max(1e-12, p))
        return -(label*math.log(clipped)+(1-label)*math.log1p(-clipped))

    daily: dict[str, list[float]] = defaultdict(list)
    for p, b, y, day in paired:
        daily[day].append((b-y)**2 - (p-y)**2)
    gains = [sum(g)/len(g) for g in daily.values()]
    mean = sum(gains)/len(gains)
    variance = (
        sum((g-mean)**2 for g in gains)/(len(gains)-1)
        if len(gains) > 1 else 0
    )
    # Observational day-block normal approximation, not an independent
    # trial or after-cost expected-value proof.
    lower = mean - 1.96 * math.sqrt(variance/len(gains)) if len(gains) > 1 else None
    return {
        "count": n,
        "days": len(daily),
        "model_brier": sum((p-y)**2 for p, _, y, _ in paired)/n,
        "market_brier": sum((b-y)**2 for _, b, y, _ in paired)/n,
        "model_log_loss": sum(loss(p, y) for p, _, y, _ in paired)/n,
        "market_log_loss": sum(loss(b, y) for _, b, y, _ in paired)/n,
        "model_ece": _ece([(p, y) for p, _, y, _ in paired]),
        "market_ece": _ece([(b, y) for _, b, y, _ in paired]),
        "brier_gain": sum(
            (b-y)**2 - (p-y)**2 for p, b, y, _ in paired
        )/n,
        "daily_brier_gain_lower_bound": lower,
        "positive_day_share": sum(g > 0 for g in gains)/len(gains),
    }
