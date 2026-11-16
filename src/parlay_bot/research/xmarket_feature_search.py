"""Training-only, source-exact XMarket feature ablation for rejected fits.

This is a research receipt, not a candidate-selection or promotion engine.
Only the already-purged *training* partition is supplied. Its last quarter
is evaluated after a second, horizon-sized purge. The validation and sealed
test partitions are never read, scored, or used to pick an ablation.
"""
from __future__ import annotations

import math

PROFILES = (
    ("full", ()),
    ("no_dispersion", ("venue_spread_ret1", "venue_up_share", "venue_spread_ret5")),
    ("no_market_momentum", ("pm_up_chg_3", "pm_up_chg_12")),
    ("native_levels_plus_candles", (
        "pm_up_chg_3", "pm_up_chg_12",
        "venue_spread_ret1", "venue_up_share", "venue_spread_ret5",
    )),
)
MIN_FIT_ROWS = 300
MIN_EVAL_TARGETS = 100
MIN_EVAL_DAYS = 7


def compare_train_only(train, *, names: tuple[str, ...], horizon_minutes: int) -> dict:
    """Measure predeclared feature groups on a purged inner training fold.

    Scores average the venue-level squared errors *within each UTC target
    timestamp* before averaging targets, so repeated venue rows do not
    pretend to be independent outcome windows. Even distinct adjacent 5m
    targets can be serially correlated, so these are exploratory scores.
    """
    import numpy as np
    import pandas as pd

    from parlay_bot.research.archive_training import (
        _fit_logistic,
        _raw_probabilities,
        _standardize_apply,
        _standardize_fit,
    )

    if horizon_minutes not in (5, 15, 60):
        raise ValueError("unsupported horizon")
    if len(names) != len(set(names)) or "y" in names:
        raise ValueError("invalid feature names")
    result = {
        "status": "insufficient_train_only_fold",
        "research_only": True,
        "can_publish_model": False,
        "uses_outer_validation_or_test": False,
        "selection_policy": "none; future locked validation required",
        "purge_minutes": horizon_minutes,
        "profiles": [],
    }
    if train.empty or not names:
        return result
    timestamps = train.index.unique().sort_values()
    if len(timestamps) < MIN_EVAL_TARGETS + 1:
        return result
    eval_start = timestamps[int(len(timestamps) * .75)]
    purge = pd.Timedelta(minutes=horizon_minutes)
    fit = train.loc[train.index < eval_start - purge]
    evaluation = train.loc[train.index >= eval_start]
    distinct_eval = evaluation.index.nunique()
    days = evaluation.index.normalize().nunique()
    result["fold"] = {
        "fit_rows": len(fit),
        "fit_latest": str(fit.index.max()) if len(fit) else None,
        "eval_rows": len(evaluation),
        "eval_targets": int(distinct_eval),
        "eval_days": int(days),
        "eval_earliest": str(eval_start),
    }
    if (len(fit) < MIN_FIT_ROWS or distinct_eval < MIN_EVAL_TARGETS
            or days < MIN_EVAL_DAYS):
        return result
    if not fit.index.max() < eval_start - purge:
        raise AssertionError("XMarket research fold crossed its purge")

    x_fit = fit[list(names)].to_numpy(dtype=float)
    x_eval = evaluation[list(names)].to_numpy(dtype=float)
    if not np.isfinite(x_fit).all() or not np.isfinite(x_eval).all():
        raise ValueError("train-only research requires complete original features")
    y_fit = fit["y"].to_numpy(dtype=float)
    y_eval = evaluation["y"].to_numpy(dtype=float)
    if (not np.isfinite(y_fit).all() or not np.isfinite(y_eval).all()
            or not np.isin(y_fit, (0.0, 1.0)).all()
            or not np.isin(y_eval, (0.0, 1.0)).all()):
        raise ValueError("invalid XMarket research labels")
    mean, std = _standardize_fit(x_fit)
    x_fit = _standardize_apply(x_fit, mean, std)
    x_eval = _standardize_apply(x_eval, mean, std)
    for name, excluded in PROFILES:
        included = [i for i, feature in enumerate(names) if feature not in excluded]
        if not included or len(included) == len(names) and name != "full":
            continue
        weights = _fit_logistic(x_fit[:, included], y_fit)
        predictions = _raw_probabilities(weights, x_eval[:, included])
        if not np.isfinite(predictions).all():
            raise ValueError("nonfinite XMarket research probability")
        errors = pd.Series(np.square(predictions - y_eval), index=evaluation.index)
        per_target = errors.groupby(level=0).mean()
        per_day = per_target.groupby(per_target.index.normalize()).mean()
        brier = float(per_target.mean())
        if not math.isfinite(brier):
            raise ValueError("nonfinite XMarket research Brier")
        result["profiles"].append({
            "name": name,
            "excluded": [feature for feature in names if feature in excluded],
            "features_used": len(included),
            "brier_by_target": round(brier, 8),
            "target_count": len(per_target),
            "days": len(per_day),
        })
    if not result["profiles"] or result["profiles"][0]["name"] != "full":
        raise AssertionError("missing XMarket research baseline")
    baseline = result["profiles"][0]["brier_by_target"]
    for profile in result["profiles"]:
        profile["brier_delta_vs_full"] = round(baseline - profile["brier_by_target"], 8)
    result["status"] = "measured_train_only"
    return result
