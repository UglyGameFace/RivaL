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
MIN_TREND_DAYS = 5  # Avoid classifying one heavy trading day as a weekly trend.
ROUNDTRIP_COST_BPS = 20.0  # Illustrative only, not actual exchange fills.


def _bucket(rows: list[dict]) -> dict:
    """Count paired scores for ALL source outcomes, directional ones only if binary."""
    n = len(rows)
    if not n:
        return {
            "events": 0, "days": 0, "binary_targets": 0,
            "brier": None, "baseline": None, "ece": None,
            "decided_events": 0, "hit_rate": None,
            "simulated_net_bps": None,
        }
    brier = sum(x["brier"] for x in rows) / n
    base = sum(x["baseline"] for x in rows) / n
    binary = [x for x in rows if x["up_rate"] in (0, 1)]
    bins: dict[int, list[dict]] = {}
    for row in binary:
        bins.setdefault(min(9, max(0, int(float(row["probability"]) * 10))), []).append(row)
    ece = (
        sum(
            len(group) / len(binary) * abs(
                sum(x["probability"] for x in group) / len(group)
                - sum(x["up_rate"] for x in group) / len(group)
            )
            for group in bins.values()
        ) if binary else None
    )
    chosen = [x for x in binary if x["pred_count"] and x["gross_bps"] is not None]
    return {
        "events": n,
        "days": len({str(x["target_opened_at"])[:10] for x in rows\n                     if x.get("target_opened_at")}),
        "binary_targets": len(binary),
        "brier": round(brier, 5), "baseline": round(base, 5),
        "ece": round(ece, 5) if ece is not None else None,
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
    if asset not in ("BTC", "ETH", "SOL", "XRP"):
        raise ValueError("unsupported asset")
    clock = _utc(now or datetime.now(UTC))
    AutonomousResearch(market, ai=None, venues=()).initialize()
    start_recent = clock - timedelta(days=7)
    start_earlier = clock - timedelta(days=14)
    with market.store.connect() as conn:
        # Max 2*4032 result rows (5m and 15m for 14d), NOT a large per-venue
        # data transfer every time a user refreshes Discord.
        math_rows = conn.execute(
            """WITH per_venue AS (
                   SELECT horizon_minutes,target_opened_at,venue,
                          AVG(model_brier) AS model_brier,
                          AVG(baseline_brier) AS baseline_brier,
                          AVG(probability_up) AS probability,
                          MIN(outcome_up) AS lowest_outcome,
                          MAX(outcome_up) AS highest_outcome,
                          AVG((realized_close/reference_close-1)*10000) AS up_bps
                   FROM perp_forward_audit
                   WHERE asset=? AND model_version=?
                     AND settled_at IS NOT NULL
                     AND settled_at>issued_at AND settled_at<=?
                     AND issued_at<=? AND issued_at<target_opened_at
                     AND target_opened_at>=?
                     AND target_opened_at<=? AND outcome_up IN (0,1)
                     AND model_brier BETWEEN 0 AND 1
                     AND baseline_brier BETWEEN 0 AND 1
                     AND probability_up BETWEEN 0 AND 1
                     AND reference_close>0 AND realized_close>0
                   GROUP BY horizon_minutes,target_opened_at,venue
               )
               SELECT horizon_minutes,target_opened_at,
                      AVG(model_brier) AS brier,
                      AVG(baseline_brier) AS baseline,
                      AVG(probability) AS probability,
                      MIN(lowest_outcome) AS lowest_outcome,
                      MAX(highest_outcome) AS highest_outcome,
                      AVG(up_bps) AS venue_mean_up_bps
               FROM per_venue
               GROUP BY horizon_minutes,target_opened_at
               ORDER BY target_opened_at DESC LIMIT 9000""",
            (asset, FORWARD_MODEL, clock.isoformat(), clock.isoformat(),
             start_earlier.isoformat(), clock.isoformat()),
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
    # Core mathematical scoring includes disagreements: discarding the whole
    # target selects by its realized outcome. The dashboard must agree with
    # authoritative venue-balanced forward-audit Brier calculations.
    # Binary calibration, directional hit rate and cost remain disagreement-free.
    disputed: dict[tuple[int, str], int] = {}
    all_scores = []
    resolved = []
    for row in math_rows:
        horizon = int(row["horizon_minutes"])
        when = str(row["target_opened_at"])
        period = "recent" if when >= start_recent.isoformat() else "prior"
        probability = float(row["probability"])
        entry = {
            "horizon_minutes": horizon, "target_opened_at": when,
            "brier": float(row["brier"]),
            "baseline": float(row["baseline"]),
            "probability": probability, "up_rate": None,
            "pred_count": 0, "correct": None, "gross_bps": None,
        }
        all_scores.append(entry)
        if row["lowest_outcome"] != row["highest_outcome"]:
            disputed[(horizon, period)] = disputed.get((horizon, period), 0) + 1
            continue
        label = int(row["lowest_outcome"])
        decision = probability >= .55 or probability <= .45
        gross_up_bps = row["venue_mean_up_bps"]
        entry.update({
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
        resolved.append(entry)
    by_horizon = {}
    for h in (5, 15):
        recent = [
            dict(x) for x in all_scores
            if x["horizon_minutes"] == h
            and str(x["target_opened_at"]) >= start_recent.isoformat()
        ]
        prior = [
            dict(x) for x in all_scores
            if x["horizon_minutes"] == h
            and str(x["target_opened_at"]) < start_recent.isoformat()
        ]
        today, previous = _bucket(recent), _bucket(prior)
        clear_recent = [
            x for x in resolved if x["horizon_minutes"] == h
            and str(x["target_opened_at"]) >= start_recent.isoformat()
        ]
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
            and today["days"] >= MIN_TREND_DAYS
            and previous["days"] >= MIN_TREND_DAYS
            and current_skill is not None and prior_skill is not None
        ):
            gain = current_skill - prior_skill
            trend = (
                "improving" if gain > 0.005
                else "worsening" if gain < -0.005
                else "unchanged"
            )
        stress = score_cost_stress(clear_recent)
        reliability = forward_reliability(clear_recent)
        reliability["disputed_targets"] = disputed.get((h, "recent"), 0)
        by_horizon[h] = {
            "forward_reliability_7d": reliability,
            "cost_stress_7d": stress,
            "last_7d": today, "prior_7d": previous,
            "disputed_targets_7d": disputed.get((h, "recent"), 0),
            "disputed_targets_prior_7d": disputed.get((h, "prior"), 0),
            "target_outcome_policy": "score_source_brier;binary_outcomes_only_for_direction",
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
        "min_trend_days": MIN_TREND_DAYS,
        "linked_venue_outcomes": True,
        "fees_assumed_bps": ROUNDTRIP_COST_BPS,
        "includes_funding": False,
        "hypothetical_funding_stressed": True,
        "backtested_pnl": False,
    }
