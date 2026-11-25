"""Live grading view: interim, read-only grades for in-flight tests.

Felix (2026-11-24): "I don't want to be in the dark anymore waiting 21
days. I want to see the grading and everything live."

This module computes, from the same recorded state the frozen evaluators
will judge at their floors, an INTERIM picture per in-flight
pre-registered test: accrual counters against the registered floors, the
current value of every qualification leg (applying the registered leg
formulas to the data accrued so far), and stratified cuts. It writes
nothing, changes no criteria, and issues no verdict: each frozen
evaluator remains the only verdict source, and below its floors its own
status stays ACCRUAL with ``verdict: None`` — the interim leg states here
are reporting, so a test can be watched drifting toward pass or kill
instead of producing a verdict from nowhere at the end.

For the fair-value Brier leg the interim state is shown under BOTH the
frozen registered bar (-0.005, which the frozen verdict will use) and
the corrected ruler from research sweep #3 (paired delta Brier < 0 with
its day-cluster interval entirely below 0), labelled reporting-only.

N3's recorder runs outside this store (its own prospective database on
a separate host process), so its counters cannot be read here; its block
says so plainly instead of inventing numbers.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import convergence_evaluation, fair_value_evaluation
from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL

_EPS = 1e-6


def _round(value, digits=6):
    return None if value is None else round(float(value), digits)


def _day_cluster_ci(pairs: list[tuple[str, float]]) -> dict | None:
    """Day-cluster interval for a mean of per-row deltas (days as units)."""
    by_day: dict[str, list[float]] = {}
    for day, value in pairs:
        by_day.setdefault(day, []).append(value)
    days = sorted(by_day)
    if len(days) < 2:
        return None
    means = [sum(by_day[d]) / len(by_day[d]) for d in days]
    centre = sum(means) / len(means)
    var = sum((m - centre) ** 2 for m in means) / (len(means) - 1)
    half = 1.96 * math.sqrt(var / len(means))
    return {"lower": _round(centre - half), "upper": _round(centre + half),
            "days": len(days)}


def _fair_value_block(venue: str, report: dict) -> dict:
    block = report["venues"][venue]
    floors = block["floors"]
    primary = block["primary"]
    ci = primary.get("delta_log_loss_ci95") or {}
    brier_ci = primary.get("delta_brier_ci95") or {}
    halves = primary.get("chronological_halves") or {}
    dll = primary.get("delta_log_loss")
    db = primary.get("delta_brier")
    legs = {
        "paired_log_loss": {
            "value": _round(dll), "ci95": ci,
            "passing_now": bool(dll is not None and dll < 0
                                and ci.get("upper") is not None
                                and ci["upper"] < 0),
            "rule": "delta log-loss < 0 with day-cluster 95% CI entirely < 0"},
        "paired_brier_frozen": {
            "value": _round(db), "bar": fair_value_evaluation.PASS_BRIER,
            "passing_now": bool(db is not None
                                and db <= fair_value_evaluation.PASS_BRIER),
            "rule": "delta Brier <= -0.005 (frozen registered leg; the "
                    "verdict at the floors uses this one)"},
        "paired_brier_corrected": {
            "value": _round(db), "ci95": brier_ci,
            "passing_now": bool(db is not None and db < 0
                                and brier_ci.get("upper") is not None
                                and brier_ci["upper"] < 0),
            "rule": "corrected ruler (sweep #3): delta Brier < 0 with "
                    "day-cluster 95% CI entirely < 0 — reporting only"},
        "both_halves_log_loss": {
            "first": _round(halves.get("first_delta_log_loss")),
            "second": _round(halves.get("second_delta_log_loss")),
            "passing_now": bool(
                (halves.get("first_delta_log_loss") or 0.0) < 0
                and (halves.get("second_delta_log_loss") or 0.0) < 0),
            "rule": "delta log-loss < 0 in both chronological halves"},
        "both_halves_call_net": {
            "first": _round(halves.get("first_call_mean_net"), 4),
            "second": _round(halves.get("second_call_mean_net"), 4),
            "passing_now": bool(
                halves.get("first_call_mean_net") is not None
                and halves["first_call_mean_net"] > 0
                and halves.get("second_call_mean_net") is not None
                and halves["second_call_mean_net"] > 0),
            "rule": "after-cost call mean net > 0 in both halves"},
    }
    return {
        "id": f"fair_value_{venue}",
        "title": f"Fair value — {venue.capitalize()}",
        "kind": "fair_value",
        "venue": venue,
        "status": block["status"],
        "verdict": block["verdict"],
        "floors": {
            "recording_days": floors["recording_days"],
            "evaluated_windows": floors["evaluated_windows"],
            "triggered_calls": floors["triggered_calls"],
        },
        "legs": legs,
        "calls": {
            "n": primary["calls"]["n"],
            "mean_net": _round(primary["calls"]["mean_net"], 4),
            "win_rate": _round(primary["calls"]["win_rate"], 4),
        },
        "observations": primary["observations"],
        "per_asset": {
            asset: {
                "delta_log_loss": _round(score.get("delta_log_loss")),
                "delta_brier": _round(score.get("delta_brier")),
                "calls": score["calls"]["n"],
                "call_mean_net": _round(score["calls"]["mean_net"], 4),
            }
            for asset, score in sorted(block["per_asset"].items())
        },
        "coverage": {
            "windows_seen": block["coverage"]["windows_seen"],
            "window_exclusions": block["coverage"]["window_exclusions"],
            "observation_exclusions": block["coverage"]["observation_exclusions"],
        },
        "note": ("Interim leg states are reporting only; the frozen "
                 "evaluator issues the verdict once, when the floors are "
                 "met. The corrected Brier ruler is shown alongside the "
                 "frozen leg Felix's registered design specifies."),
    }


def _convergence_block(report: dict, now: datetime) -> dict:
    coverage = report["coverage"]
    pooled = report["cuts"]["pooled"]
    halves = pooled.get("chronological_halves") or {}
    mean_net = pooled.get("mean_net_per_contract")
    deadline = convergence_evaluation.ACCRUAL_DEADLINE
    legs = {
        "mean_net": {
            "value": _round(mean_net, 4),
            "bar": convergence_evaluation.PASS_MEAN_NET,
            "passing_now": bool(mean_net is not None
                                and mean_net > convergence_evaluation.PASS_MEAN_NET),
            "rule": "mean net per contract > +0.5c after costs"},
        "both_halves_net": {
            "first": _round(halves.get("first_mean"), 4),
            "second": _round(halves.get("second_mean"), 4),
            "first_n": halves.get("first_n"), "second_n": halves.get("second_n"),
            "passing_now": bool(
                halves.get("first_mean") is not None
                and halves["first_mean"] > 0
                and halves.get("second_mean") is not None
                and halves["second_mean"] > 0),
            "rule": "mean net > 0 in both chronological halves"},
    }
    return {
        "id": "convergence",
        "title": "PM/Kalshi convergence (lag)",
        "kind": "convergence",
        "venue": "polymarket+kalshi",
        "status": report["status"],
        "verdict": report["verdict"],
        "floors": {
            "trigger_windows": {
                "have": coverage["trigger_windows"],
                "need": convergence_evaluation.MIN_TRIGGER_WINDOWS},
            "distinct_days": {
                "have": coverage["trigger_distinct_days"],
                "need": convergence_evaluation.MIN_DISTINCT_DAYS},
        },
        "accrual_deadline": deadline.isoformat(),
        "days_to_deadline": max(
            0, (deadline.date() - now.date()).days),
        "legs": legs,
        "day_block_interval": pooled.get("day_block_interval"),
        "win_rate": _round(pooled.get("win_rate"), 4),
        "per_asset": {
            asset: {"n": cut["n"],
                    "mean_net": _round(cut.get("mean_net_per_contract"), 4)}
            for asset, cut in sorted(report["cuts"]["per_asset"].items())
        },
        "coverage": {
            "windows_seen": coverage["windows_seen"],
            "exclusions": coverage["exclusions"],
            "fee_verified_rate": _round(coverage.get("fee_verified_rate"), 4),
        },
        "note": ("Interim leg states are reporting only. Known frozen-test "
                 "defect (reported 2026-11-24, running test unchanged): "
                 "recorded fee rates are the raw Gamma schedule rate, "
                 "understating the registered band rate by ~0.6c/trade on "
                 "band-interior fills, so interim nets read slightly "
                 "better than the registered basis."),
    }


def _live_model_block(store, now: datetime) -> dict:
    """Rolling grade of the live per-stream logistic vs its baseline.

    The baseline is the issuance-time reference probability recorded on
    the same row (the market-implied comparator the audit used). Perp
    forecasts carry no venue fee; the comparison is paired log-loss and
    Brier on identical settled rows.
    """
    since = (now - timedelta(days=8)).isoformat()
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT issued_at, horizon_minutes, probability_up, baseline_up,
                      outcome_up, model_brier, baseline_brier
               FROM perp_forward_audit
               WHERE model_version=? AND settled_at IS NOT NULL
                 AND outcome_up IN (0,1) AND issued_at>=?""",
            (LIVE_MODEL, since)).fetchall()

    def _grade(selected) -> dict:
        dll_pairs, db_pairs = [], []
        right = base_right = 0
        for row in selected:
            p = min(max(float(row["probability_up"]), _EPS), 1 - _EPS)
            b = min(max(float(row["baseline_up"]), _EPS), 1 - _EPS)
            y = int(row["outcome_up"])
            ll_model = -(y * math.log(p) + (1 - y) * math.log(1 - p))
            ll_base = -(y * math.log(b) + (1 - y) * math.log(1 - b))
            day = str(row["issued_at"])[:10]
            dll_pairs.append((day, ll_model - ll_base))
            db_pairs.append((day, float(row["model_brier"])
                             - float(row["baseline_brier"])))
            right += int((p > 0.5 and y == 1) or (p < 0.5 and y == 0))
            base_right += int((b > 0.5 and y == 1) or (b < 0.5 and y == 0))
        n = len(selected)
        if not n:
            return {"settled": 0, "delta_log_loss": None,
                    "delta_log_loss_ci95": None, "delta_brier": None,
                    "delta_brier_ci95": None, "accuracy": None,
                    "baseline_accuracy": None, "days": 0}
        return {
            "settled": n,
            "delta_log_loss": _round(sum(v for _, v in dll_pairs) / n),
            "delta_log_loss_ci95": _day_cluster_ci(dll_pairs),
            "delta_brier": _round(sum(v for _, v in db_pairs) / n),
            "delta_brier_ci95": _day_cluster_ci(db_pairs),
            "accuracy": _round(right / n, 4),
            "baseline_accuracy": _round(base_right / n, 4),
            "days": len({d for d, _ in dll_pairs}),
        }

    rows = list(rows)
    day_ago = (now - timedelta(days=1)).isoformat()
    week = [r for r in rows if str(r["issued_at"]) >= (now - timedelta(days=7)).isoformat()]
    return {
        "id": "live_model",
        "title": "Live model (per-stream logistic) — rolling grade",
        "kind": "live_model",
        "last_24h": _grade([r for r in rows if str(r["issued_at"]) >= day_ago]),
        "last_7d": _grade(week),
        "per_horizon_7d": {
            str(h): _grade([r for r in week if int(r["horizon_minutes"]) == h])
            for h in sorted({int(r["horizon_minutes"]) for r in week})
        },
        "note": ("Paired against the issuance-time baseline probability "
                 "on the same settled rows. Negative deltas are better. "
                 "This is the production model's record, not a test."),
    }


def read_live_grading(store, now: datetime | None = None) -> dict:
    """The full live-grading section; each block fails independently."""
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    out: dict = {"schema": "rival-live-grading-v1",
                 "computed_at": clock.isoformat(), "tests": []}
    try:
        fv = fair_value_evaluation.evaluate_fair_value(
            fair_value_evaluation.load_windows(store), now=clock)
        for venue in fair_value_evaluation.VENUES:
            out["tests"].append(_fair_value_block(venue, fv))
    except Exception:  # noqa: BLE001 - one block must not sink the receipt
        out["tests"].append({"id": "fair_value", "kind": "fair_value",
                             "status": "read_unavailable"})
    try:
        conv = convergence_evaluation.evaluate_windows(
            convergence_evaluation.load_windows(store), now=clock)
        out["tests"].append(_convergence_block(conv, clock))
    except Exception:  # noqa: BLE001
        out["tests"].append({"id": "convergence", "kind": "convergence",
                             "status": "read_unavailable"})
    out["tests"].append({
        "id": "n3", "title": "N3 — post-determination band (PM crypto)",
        "kind": "n3", "venue": "polymarket", "status": "external_recorder",
        "live_counters": None,
        "registered": {
            "band": [0.90, 0.97], "deadline": "2027-03-31",
            "rule": "calls accrue from BUY fills of the winning outcome "
                    "inside the band after determination"},
        "note": ("N3's recorder runs outside the bot's database (its own "
                 "prospective store), so this receipt cannot read its "
                 "counters. Last verified census (2026-11-24): 4 calls, "
                 "all winners; accrual ~1.1/day against a ~2.7/day "
                 "expectation — verified market reality, not a recorder "
                 "fault. Wiring its counters in needs a publisher from "
                 "the N3 host."),
    })
    try:
        out["live_model"] = _live_model_block(store, clock)
    except Exception:  # noqa: BLE001
        out["live_model"] = {"id": "live_model", "status": "read_unavailable"}
    return out
