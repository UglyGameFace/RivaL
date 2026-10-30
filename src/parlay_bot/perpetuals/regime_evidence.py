"""Market-regime evidence for immutable, genuinely prospective predictions.

Regimes are chosen at issuance from already-available closed candles.
Multiple venues forecasting the same UTC target are correlated, not N bets.
Qualified *shadow* comparisons never imply a profitable tradable strategy.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

VERSION = "causal-volatility-5m-v1"
REGIMES = ("quiet", "normal", "turbulent")
MIN_REGIME_TARGETS = 50
MIN_REGIME_DAYS = 7
MIN_PERIOD_TARGETS = 10
MIN_PERIODS = 2
MIN_DISTINCT_REGIMES = 2


def issue_regime(features: tuple[float, ...]) -> str:
    """Predeclared thresholds on five-minute log-return volatility in pct.

    Only the first five features (the original return/volatility vector)
    decide the regime; later indicator features are ignored here.
    """
    if len(features) < 5 or any(not math.isfinite(v) for v in features):
        raise ValueError("invalid point-in-time features")
    volatility_pct = features[2]
    if volatility_pct < 0:
        raise ValueError("negative volatility")
    if volatility_pct < 0.10:
        return "quiet"
    if volatility_pct < 0.35:
        return "normal"
    return "turbulent"


def _time(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("naive prospective time")
    stamp = stamp.astimezone(UTC)
    if stamp.isoformat() != value:
        raise ValueError("noncanonical prospective UTC time")
    return stamp


def evaluate_regimes(
    rows: list[dict[str, Any]], *, as_of: datetime | None = None,
) -> dict[str, Any]:
    """Compare paired model/baseline accuracy across distinct later regimes.

    The join providing rows must include ONLY prospectively issued and settled
    records, linked to their immutable at-issuance regime. This function still
    verifies order, finite losses and uniqueness to fail closed.
    """
    now = as_of or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("naive audit clock")
    now = now.astimezone(UTC)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        target = _time(row["target_opened_at"])
        issued = _time(row["issued_at"])
        settled = _time(row["settled_at"])
        if not issued < target + timedelta(minutes=5) < settled <= now:
            continue
        regime = str(row["regime"])
        if regime not in REGIMES:
            continue
        m, b = float(row["model_brier"]), float(row["baseline_brier"])
        if not (math.isfinite(m) and math.isfinite(b)
                and 0 <= m <= 1 and 0 <= b <= 1):
            continue
        grouped.setdefault(target.isoformat(), []).append({
            "regime": regime, "model": m, "baseline": b, "target": target,
        })
    eligible: list[dict[str, Any]] = []
    disputed = 0
    for target, entries in sorted(grouped.items()):
        # A UTC target is not independently observed ten times. If venues
        # disagree about regime, exclude that target from regime qualification.
        tags = {x["regime"] for x in entries}
        if len(tags) != 1:
            disputed += 1
            continue
        eligible.append({
            "target": target,
            "regime": next(iter(tags)),
            "day": entries[0]["target"].date().isoformat(),
            "week": (entries[0]["target"].date()
                     - timedelta(days=entries[0]["target"].weekday())).isoformat(),
            "model": sum(x["model"] for x in entries) / len(entries),
            "baseline": sum(x["baseline"] for x in entries) / len(entries),
        })
    reports = {}
    mature_regimes = 0
    for regime in REGIMES:
        data = [x for x in eligible if x["regime"] == regime]
        days = len({x["day"] for x in data})
        week_groups: dict[str, list[float]] = {}
        for item in data:
            week_groups.setdefault(item["week"], []).append(
                item["baseline"] - item["model"]
            )
        # Require distinct subsequent calendar weeks. A single lucky
        # weekend with repeated candles is not "consistent improvement."
        periods = [
            sum(v) / len(v) for v in week_groups.values()
            if len(v) >= MIN_PERIOD_TARGETS
        ]
        mean_gain = (
            sum(x["baseline"] - x["model"] for x in data) / len(data)
            if data else None
        )
        eligible_regime = (
            len(data) >= MIN_REGIME_TARGETS
            and days >= MIN_REGIME_DAYS and len(periods) >= MIN_PERIODS
        )
        passed = (
            eligible_regime and mean_gain is not None
            and mean_gain > 0.005
            and all(p > 0 for p in periods)
        )
        mature_regimes += int(eligible_regime)
        reports[regime] = {
            "unique_targets": len(data), "utc_days": days,
            "qualified_weeks": len(periods),
            "positive_weeks": sum(x > 0 for x in periods),
            "paired_brier_gain": round(mean_gain, 6) if mean_gain is not None else None,
            "status": ("insufficient_forward_evidence" if not eligible_regime
                       else "stable_shadow_gain" if passed
                       else "unstable_or_negative_skill"),
        }
    all_required = [
        x for x in reports.values()
        if x["status"] != "insufficient_forward_evidence"
    ]
    stable = (
        mature_regimes >= MIN_DISTINCT_REGIMES
        and all(x["status"] == "stable_shadow_gain" for x in all_required)
    )
    return {
        "method": VERSION,
        "unique_target_windows": len(eligible),
        "disputed_regime_targets": disputed,
        "regimes": reports,
        "status": (
            "consistent_shadow_skill_research_only" if stable
            else "insufficient_distinct_market_conditions"
            if mature_regimes < MIN_DISTINCT_REGIMES
            else "regime_instability_detected"
        ),
        # Actual costs depend on real fills, fees, slippage and funding.
        "actual_execution_costs_measured": False,
        "strategy_profitability_verified": False,
        "trading_approved": False,
    }
