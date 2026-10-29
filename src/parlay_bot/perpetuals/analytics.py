"""On-demand PostgreSQL analytics for two independent research tracks.

Same-time exchanges are correlated; score one UTC target event, not ten
pretend independent predictions. All results are research, not trade PnL.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.autonomous import AutonomousResearch, _utc
from parlay_bot.perpetuals.calibration_audit import forward_reliability
from parlay_bot.perpetuals.cost_stress import score_cost_stress
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

MIN_TREND_EVENTS = 100
ROUNDTRIP_COST_BPS = 20.0  # Illustrative only, not actual exchange fills.


def _bucket(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {
            "events": 0, "brier": None, "baseline": None,
            "ece": None, "decided_events": 0, "hit_rate": None,
            "simulated_net_bps": None,
        }
    brier = sum(x["brier"] for x in rows) / n
    base = sum(x["baseline"] for x in rows) / n
    # Genuine ten-bin expected calibration error (ECE) rather than
    # per-event absolute error mislabeled as calibration.
    bins: dict[int, list[dict]] = {}
    for row in rows:
        bins.setdefault(min(9, max(0, int(float(row["probability"]) * 10))), []).append(row)
    calib = sum(
        (len(group) / n) * abs(
            sum(float(x["probability"]) for x in group) / len(group)
            - sum(float(x["up_rate"]) for x in group) / len(group)
        )
        for group in bins.values()
    )
    chosen = [x for x in rows if x["pred_count"] and x["gross_bps"] is not None]
    return {
        "events": n,
        "brier": round(brier, 5), "baseline": round(base, 5),
        "ece": round(calib, 5),
        "decided_events": len(chosen),
        "hit_rate": (
            round(sum(x["correct"] for x in chosen) / len(chosen), 4)
            if chosen else None
        ),
        "simulated_net_bps": (
            round(sum(x["gross_bps"] - ROUNDTRIP_COST_BPS for x in chosen)
                  / len(chosen), 3) if chosen else None
        ),
    }


def analytics_snapshot(
    market: PerpetualMarketStore, *, asset: str,
    now: datetime | None = None,
) -> dict:
    if asset not in ("BTC", "ETH", "SOL"):
        raise ValueError("unsupported asset")
    clock = _utc(now or datetime.now(UTC))
    AutonomousResearch(market, ai=None, venues=()).initialize()
    start_recent = clock - timedelta(days=7)
    start_earlier = clock - timedelta(days=14)
    with market.store.connect() as conn:
        # Max 2*4032 result rows (5m and 15m for 14d), NOT a large per-venue
        # data transfer every time a user refreshes Discord.
        math_rows = conn.execute(
            """SELECT horizon_minutes,target_opened_at,
                      AVG(model_brier) AS brier,
                      AVG(baseline_brier) AS baseline,
                      AVG(probability_up) AS probability,
                      MIN(outcome_up) AS lowest_outcome,
                      MAX(outcome_up) AS highest_outcome,
                      AVG((realized_close/reference_close-1)*10000)
                          AS venue_mean_up_bps
               FROM perp_forward_audit
               WHERE asset=? AND model_version=? AND settled_at IS NOT NULL
                 AND target_opened_at>=? AND target_opened_at<=?
                 AND outcome_up IS NOT NULL
               GROUP BY horizon_minutes,target_opened_at
               ORDER BY target_opened_at DESC LIMIT 9000""",
            (asset, FORWARD_MODEL, start_earlier.isoformat(), clock.isoformat()),
        ).fetchall()
        ai_rows = conn.execute(
            """SELECT model,issued_at,direction,settled_at,correct
               FROM perp_auto_ai_forecasts
               WHERE asset=? AND issued_at>=? AND issued_at<=?
               ORDER BY issued_at DESC LIMIT 150""",
            (asset, start_earlier.isoformat(), clock.isoformat()),
        ).fetchall()
        governors = conn.execute(
            """SELECT horizon_minutes,status,reason,unique_target_windows,
                      calendar_days,candidate_brier,baseline_brier,evaluated_at
               FROM perp_model_governance WHERE asset=?
               ORDER BY evaluated_at DESC LIMIT 30""",
            (asset,),
        ).fetchall()
        pending = conn.execute(
            """SELECT COUNT(*) AS n FROM perp_forward_audit
               WHERE asset=? AND model_version=? AND settled_at IS NULL""",
                (asset, FORWARD_MODEL),
        ).fetchone()
    # Every UTC target is ONE correlated event across providers. Never
    # interpret opposite settled venue labels as a fractional win. Show
    # disputed targets separately and exclude them from ALL scoring,
    # calibration, hypothetical cost results and improvement decisions.
    disputed: dict[tuple[int, str], int] = {}
    resolved = []
    for row in math_rows:
        horizon = int(row["horizon_minutes"])
        when = str(row["target_opened_at"])
        period = "recent" if when >= start_recent.isoformat() else "prior"
        if row["lowest_outcome"] != row["highest_outcome"]:
            disputed[(horizon, period)] = disputed.get((horizon, period), 0) + 1
            continue
        probability = float(row["probability"])
        label = int(row["lowest_outcome"])
        decision = probability >= .55 or probability <= .45
        gross_up_bps = row["venue_mean_up_bps"]
        resolved.append({
            "horizon_minutes": horizon, "target_opened_at": when,
            "brier": float(row["brier"]),
            "baseline": float(row["baseline"]),
            "probability": probability,
            "up_rate": label,
            "pred_count": int(decision),
            "correct": (
                int((probability >= .55) == bool(label))
                if decision else None
            ),
            "gross_bps": (
                float(gross_up_bps) * (1 if probability >= .55 else -1)
                if decision and gross_up_bps is not None else None
            ),
        })
    by_horizon = {}
    for h in (5, 15):
        recent = [
            dict(x) for x in resolved
            if x["horizon_minutes"] == h
            and str(x["target_opened_at"]) >= start_recent.isoformat()
        ]
        prior = [
            dict(x) for x in resolved
            if x["horizon_minutes"] == h
            and str(x["target_opened_at"]) < start_recent.isoformat()
        ]
        today, previous = _bucket(recent), _bucket(prior)
        # Comparing raw Brier scores across weeks can label a model as
        # "improving" simply because the market became easier to predict.
        # Compare each week's model-vs-baseline *paired skill* instead.
        current_skill = (
            today["baseline"] - today["brier"]
            if today["baseline"] is not None else None
        )
        prior_skill = (
            previous["baseline"] - previous["brier"]
            if previous["baseline"] is not None else None
        )
        trend = "insufficient"
        if (
            today["events"] >= MIN_TREND_EVENTS
            and previous["events"] >= MIN_TREND_EVENTS
            and current_skill is not None and prior_skill is not None
        ):
            gain = current_skill - prior_skill
            trend = (
                "improving" if gain > 0.005
                else "worsening" if gain < -0.005
                else "unchanged"
            )
        stress = score_cost_stress(recent)
        reliability = forward_reliability(recent)
        by_horizon[h] = {
            "forward_reliability_7d": reliability,
            "cost_stress_7d": stress,
            "last_7d": today, "prior_7d": previous,
            "disputed_targets_7d": disputed.get((h, "recent"), 0),
            "disputed_targets_prior_7d": disputed.get((h, "prior"), 0),
            "target_outcome_policy": "exclude_disagreeing_exchange_labels",
            "trend": trend, "last_7d_skill": current_skill,
            "prior_7d_skill": prior_skill,
            "trend_method": "relative_to_same_window_baseline",
        }
    seen = set()
    governance = {}
    for row in governors:
        h = int(row["horizon_minutes"])
        if h not in seen:
            governance[h] = dict(row)
            seen.add(h)
    recent_ai = [
        r for r in ai_rows
        if str(r["issued_at"]) >= start_recent.isoformat()
    ]
    settled = [r for r in recent_ai if r["settled_at"] is not None]
    decided = [r for r in settled if r["correct"] is not None]
    return {
        "asset": asset, "as_of": clock.isoformat(),
        "math": by_horizon,
        "model_governance": governance,
        "math_pending": int(pending["n"]),
        "gemini": {
            "issued": len(recent_ai),
            "settled": len(settled),
            "decided": len(decided),
            "abstained": sum(x["direction"] == "uncertain" for x in settled),
            "directional_accuracy": (
                round(sum(int(x["correct"]) for x in decided) / len(decided), 4)
                if decided else None
            ),
        },
        "min_trend_events": MIN_TREND_EVENTS,
        "linked_venue_outcomes": True,
        "fees_assumed_bps": ROUNDTRIP_COST_BPS,
        "includes_funding": False,
        "hypothetical_funding_stressed": True,
        "backtested_pnl": False,
    }
