"""Late-window settlement-mechanics test (pre-registered 2026-11-27).

Grades ONE narrow claim, frozen in
``rival-late-window-mechanics-preregistration-20261127.md``: on
Polymarket 15-minute Up/Down windows, the parent fair-value model's
calls at t=840s only (parent design section 6.4 call rule, unchanged)
earn a positive mean net after full taker costs on prospective data.

Everything load-bearing is inherited from the parent evaluator
(``fair_value_evaluation``): the fair value itself, the checkpoint
observation, the call rule, the fee basis, and the day-clustered
bootstrap. This module adds only the epoch filter, the calls-only
floors and legs, and the verdict state machine. It deliberately has NO
probability leg: the parent test owns that question and answered it
(Polymarket t=840s paired delta-log-loss was +0.138 -- worse than the
market -- on the pre-registration receipt). This test claims the call
subset only, and a PASS here does not rehabilitate the parent model.

Pure functions over the parent's window dicts; the CLI in
``tools/evaluate_late_window.py`` supplies the store. Read-only.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fair_value_evaluation import (
    _day_cluster_interval,
    _observe,
)

# Frozen at registration (2026-11-27, owner's tap 22:44Z). The epoch is
# the first full minute after registration; a window counts only when
# its DECISION time (window_open + 840s) is at or after the epoch.
REGISTRATION_EPOCH = datetime(2026, 11, 27, 22, 45, tzinfo=UTC)
ACCRUAL_DEADLINE = datetime(2027, 1, 31, 23, 59, 59, tzinfo=UTC)
VENUE = "polymarket"
CHECKPOINT_OFFSET = 840

MIN_CALLS = 150
MIN_DAYS = 21
MIN_CALLS_PER_HALF = 50
ADVERSE_MAX = 0.15
ADVERSE_NET = -0.90


def collect_observations(windows: list[dict]) -> tuple[list[dict], dict]:
    """Post-epoch Polymarket t=840s observations from recorded windows.

    Mirrors the parent evaluator's fail-closed discipline: a window
    missing its settlement is excluded and counted; checkpoint-level
    exclusions (index gap, missing snapshot, unfillable leg, unverified
    fee) are counted by the parent's own observation builder.
    """
    exclusions: Counter = Counter()
    observations: list[dict] = []
    for window in windows:
        if window.get("venue") != VENUE:
            continue
        opened = window["window_open"]
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        if opened + timedelta(seconds=CHECKPOINT_OFFSET) < REGISTRATION_EPOCH:
            exclusions["pre_epoch"] += 1
            continue
        if window.get("settlement") is None:
            exclusions["settlement_unavailable"] += 1
            continue
        if window.get("strike") is None:
            exclusions["strike_unavailable"] += 1
            continue
        obs = _observe(window, CHECKPOINT_OFFSET,
                       observation_exclusions=exclusions)
        if obs is not None:
            observations.append(obs)
    return observations, dict(exclusions)


def _halves(calls: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(calls, key=lambda o: o["window_open"])
    split = len(ordered) // 2
    return ordered[:split], ordered[split:]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def grade_late_window(observations: list[dict], *, now: datetime,
                      exclusions: dict | None = None) -> dict:
    """The frozen verdict state machine over t=840s observations.

    ``observations`` are the parent's observation dicts (see
    ``collect_observations``); only rows with a call carry a
    ``call_net``. Below the floors the status is ACCRUAL (or
    ACCRUAL_FAILED past the deadline); at full floors the three frozen
    legs decide PASS / KILL. Standing numbers during accrual are
    reported as preliminary and are never a verdict.
    """
    calls = [o for o in observations if o.get("call_side") is not None]
    days = sorted({o["day"] for o in calls})
    first, second = _halves(calls)
    nets = [float(o["call_net"]) for o in calls]
    mean_net = _mean(nets)
    interval = _day_cluster_interval(calls, "call_net") if len(days) >= 2 else None
    adverse = (sum(1 for n in nets if n <= ADVERSE_NET) / len(nets)
               if nets else None)
    floors = {
        "calls": {"have": len(calls), "need": MIN_CALLS},
        "distinct_days": {"have": len(days), "need": MIN_DAYS},
        "calls_first_half": {"have": len(first), "need": MIN_CALLS_PER_HALF},
        "calls_second_half": {"have": len(second), "need": MIN_CALLS_PER_HALF},
    }
    floors_met = all(f["have"] >= f["need"] for f in floors.values())
    report: dict = {
        "status": "ACCRUAL",
        "verdict": None,
        "venue": VENUE,
        "checkpoint_offset": CHECKPOINT_OFFSET,
        "epoch": REGISTRATION_EPOCH.isoformat(),
        "evaluated_windows": len(observations),
        "calls": len(calls),
        "distinct_days": len(days),
        "floors": floors,
        "exclusions": exclusions or {},
        "standing": {
            "mean_net": mean_net,
            "mean_net_ci95": interval,
            "first_half_mean_net": _mean([float(o["call_net"]) for o in first]),
            "second_half_mean_net": _mean([float(o["call_net"]) for o in second]),
            "adverse_share": adverse,
            "preliminary": not floors_met,
        },
    }
    if not floors_met:
        if now > ACCRUAL_DEADLINE:
            report["status"] = "ACCRUAL_FAILED"
        return report
    legs = {
        "mean_net_ci_above_zero": bool(
            mean_net is not None and mean_net > 0
            and interval is not None and interval["lower"] > 0),
        "both_halves_mean_net_positive": bool(
            report["standing"]["first_half_mean_net"] is not None
            and report["standing"]["first_half_mean_net"] > 0
            and report["standing"]["second_half_mean_net"] is not None
            and report["standing"]["second_half_mean_net"] > 0),
        "adverse_share_within_cap": bool(
            adverse is not None and adverse <= ADVERSE_MAX),
    }
    report["legs"] = legs
    report["standing"]["preliminary"] = False
    report["verdict"] = "PASS" if all(legs.values()) else "KILL"
    report["status"] = "EVALUATED"
    return report


def evaluate_late_window(windows: list[dict], *, now: datetime) -> dict:
    """Collect post-epoch observations and grade them (pure, no I/O)."""
    observations, exclusions = collect_observations(windows)
    return grade_late_window(observations, now=now, exclusions=exclusions)
