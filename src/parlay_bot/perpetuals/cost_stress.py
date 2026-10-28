"""Conservative, cost-stressed research on truly settled prospective outcomes.

Never use in-sample backtests or count ten correlated venues as ten independent
trades. All costs are hypothetical stress assumptions, NOT observed fills,
funding debits, maker/taker tier data, margin or liquidation modeling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

MIN_UNIQUE_TARGETS = 100
MIN_UTC_DAYS = 7
DAILY_STRESS_MULTIPLIER = 2.58


@dataclass(frozen=True)
class CostScenario:
    label: str
    round_trip_fees_bps: float
    entry_slippage_bps: float
    exit_slippage_bps: float
    adverse_funding_bps: float

    @property
    def total_bps(self) -> float:
        return (
            self.round_trip_fees_bps
            + self.entry_slippage_bps
            + self.exit_slippage_bps
            + self.adverse_funding_bps
        )


SCENARIOS = (
    CostScenario("low_assumption", 8.0, 3.0, 3.0, 2.0),
    CostScenario("moderate_stress", 15.0, 8.0, 8.0, 9.0),
    CostScenario("severe_stress", 25.0, 15.0, 15.0, 25.0),
)


def score_cost_stress(
    rows: list[dict], *,
    scenarios: tuple[CostScenario, ...] = SCENARIOS,
) -> dict:
    """Unique target-based forward-only net-return research.

    Requires each row to have an actual settled directional decision.
    A target with multiple correlated venues is averaged once. A no-trade
    target contributes no return; it cannot inflate positive hit rates.
    """
    if not scenarios or len({s.label for s in scenarios}) != len(scenarios):
        raise ValueError("invalid cost scenarios")
    for scenario in scenarios:
        fields = (
            scenario.round_trip_fees_bps, scenario.entry_slippage_bps,
            scenario.exit_slippage_bps, scenario.adverse_funding_bps,
        )
        if any(not math.isfinite(x) or x < 0 for x in fields):
            raise ValueError("invalid assumed trading costs")
    targets: dict[str, list[float]] = {}
    for item in rows:
        target = str(item["target_opened_at"])
        ts = datetime.fromisoformat(target)
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("non-UTC prospective target")
        if ts.astimezone(UTC).isoformat() != target:
            raise ValueError("noncanonical UTC prospective target")
        # 'pred_count' may be fractional because venue-level decisions were
        # aggregated by target. Only use actually decided venues, not raw
        # neutral 0.5 probabilities.
        n = float(item["pred_count"] or 0)
        gross = item["gross_bps"]
        if n <= 0 or gross is None:
            continue
        value = float(gross)
        if not math.isfinite(value):
            raise ValueError("invalid forward gross return")
        targets.setdefault(target, []).append(value)
    # Multiple venue results recorded at the same UTC horizon are
    # correlated, not independent bankroll bets.
    unique = {
        target: sum(values) / len(values)
        for target, values in targets.items()
    }
    days = {datetime.fromisoformat(x).date().isoformat() for x in unique}
    reports = {}
    for scenario in scenarios:
        net = {
            target: gross - scenario.total_bps
            for target, gross in unique.items()
        }
        daily: dict[str, list[float]] = {}
        for target, value in net.items():
            day = datetime.fromisoformat(target).date().isoformat()
            daily.setdefault(day, []).append(value)
        daily_means = [
            sum(values) / len(values)
            for _, values in sorted(daily.items())
        ]
        n_days = len(daily_means)
        mean_daily = sum(daily_means) / n_days if n_days else None
        daily_sd = (
            math.sqrt(sum((v - mean_daily) ** 2 for v in daily_means)
                      / (n_days - 1))
            if n_days > 1 and mean_daily is not None else None
        )
        stress_lower = (
            mean_daily - DAILY_STRESS_MULTIPLIER * daily_sd / math.sqrt(n_days)
            if daily_sd is not None and mean_daily is not None else None
        )
        net_mean = sum(net.values()) / len(net) if net else None
        positive_days = sum(x > 0 for x in daily_means)
        ready = len(unique) >= MIN_UNIQUE_TARGETS and n_days >= MIN_UTC_DAYS
        status = (
            "insufficient_forward_outcomes" if not ready
            else "fails_cost_stress" if (
                net_mean is None or net_mean <= 0
                or stress_lower is None or stress_lower <= 0
                or positive_days < math.ceil(n_days * 0.8)
            )
            else "hypothetical_stress_pass_not_approved"
        )
        reports[scenario.label] = {
            "assumed_total_bps": scenario.total_bps,
            "mean_net_bps": round(net_mean, 3) if net_mean is not None else None,
            "daily_stress_lower_bps": (
                round(stress_lower, 3) if stress_lower is not None else None
            ),
            "positive_days": positive_days,
            "status": status,
        }
    return {
        "unique_decided_targets": len(unique),
        "utc_days": len(days),
        "scenarios": reports,
        "funding_measured": False,
        "actual_fills_measured": False,
        "liquidation_modeled": False,
        "live_trading_approved": False,
        "research_only": True,
    }
