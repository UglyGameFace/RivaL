"""Offline, research-only volatility blend and separate-target diagnostics.

This independently tests 24h/7d/30d *five-minute* return-variance
estimates against the same future outcomes. No uploaded weights, execution
quotes, market-contract outcomes, or profitable-trading claims are imported.

The diagnostic is attached to the existing VolCal training report, never to
a live probability/serving decision. Only completed, contiguous five-minute
candles qualify. Blend weights are selected on validation, tested once on an
unseen chronological holdout, and remain ineligible for model promotion.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

FIVE_MINUTES = timedelta(minutes=5)
WINDOWS = (288, 2016, 8640)  # 24 hours, 7 days, 30 days of 5m returns.
CANDIDATES = (
    (1.0, 0.0, 0.0),  # single-window 24h baseline
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.6, 0.3, 0.1),
    (0.2, 0.3, 0.5),
    (1 / 3, 1 / 3, 1 / 3),
)
MIN_WINDOW_ROWS = 200
MIN_WINDOW_DAYS = 7
MIN_REGIME_ROWS = 75
Z90 = 1.6448536269514722


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("research window must be timezone-aware")
    return value.astimezone(UTC)


def _source(candles, *, steps: int, train_end: datetime):
    """Return issuance-time predictors and independent future labels.

    Timestamps identify a five-minute bar's CLOSE, not the start of a
    price observation. Requiring every 5m gap avoids applying a 30-day
    rolling statistic to a sparse or stitched historical month.
    """
    import numpy as np
    import pandas as pd

    if not isinstance(candles.index, pd.DatetimeIndex):
        return None
    if candles.index.tz is None or candles.index.has_duplicates:
        return None
    if not candles.index.is_monotonic_increasing:
        return None
    warm_start = train_end - timedelta(days=32)
    candles = candles.loc[candles.index >= warm_start]
    if len(candles) < WINDOWS[-1] + steps + 1:
        return None
    closes = pd.to_numeric(candles["close"], errors="coerce").astype(float)
    closes = closes.where((closes > 0) & np.isfinite(closes))
    contiguous = candles.index.to_series().diff().eq(pd.Timedelta(minutes=5))
    log_return = np.log(closes).diff().where(contiguous)
    variances = [log_return.rolling(n, min_periods=n).var(ddof=0)
                 for n in WINDOWS]
    future_ok = pd.Series(True, index=candles.index)
    for step in range(1, steps + 1):
        future_ok &= contiguous.shift(-step, fill_value=False)
    future_return = np.log(closes.shift(-steps) / closes).where(future_ok)
    frame = pd.DataFrame({
        "realized": future_return,
        "v24": variances[0],
        "v7": variances[1],
        "v30": variances[2],
    }, index=candles.index)
    # Timing means the first *future 5-minute close* with an absolute
    # move of at least one 24h-derived horizon sigma. Intrabar high/low
    # first passage is not inferable from OHLC; do not invent it.
    threshold = np.sqrt(frame["v24"] * steps)
    first = pd.Series(0, index=candles.index, dtype="int64")
    for step in range(1, steps + 1):
        move = np.abs(np.log(closes.shift(-step) / closes))
        hit = future_ok & (threshold > 0) & (first == 0) & (move >= threshold)
        first.loc[hit] = step
    frame["first_cross_bars"] = first.where(future_ok)
    ratio = np.sqrt(frame["v24"] / frame["v7"].where(frame["v7"] > 0))
    ratio = ratio.where(np.isfinite(ratio), 1.0)
    frame["regime"] = np.select((ratio < 0.8, ratio > 1.2),
                                 ("calm", "turbulent"), default="normal")
    frame = frame.replace([np.inf, -np.inf], np.nan)
    return frame.dropna(subset=("realized", "v24", "v7", "v30", "first_cross_bars"))


def _variance(frame, weights, steps: int):
    import numpy as np

    return np.maximum(1e-10, steps * (
        frame["v24"].to_numpy() * weights[0]
        + frame["v7"].to_numpy() * weights[1]
        + frame["v30"].to_numpy() * weights[2]))


def _nll(frame, weights, steps: int) -> float:
    import numpy as np

    variance = _variance(frame, weights, steps)
    realized = frame["realized"].to_numpy()
    return float(np.mean(0.5 * (np.log(2 * math.pi * variance)
                                  + realized * realized / variance)))


def _days(frame) -> int:
    return len(set(frame.index.date))


def _metric(frame, weights, steps: int) -> dict:
    import numpy as np

    variance = _variance(frame, weights, steps)
    realized = frame["realized"].to_numpy()
    limit = Z90 * np.sqrt(variance)
    return {
        "nll": round(_nll(frame, weights, steps), 8),
        "central_90_interval_coverage": round(float(np.mean(np.abs(realized) <= limit)), 5),
        "mean_central_90_interval_width_log_return": round(float(np.mean(2 * limit)), 8),
    }


def _select(frame, steps: int):
    # Deterministic, predeclared grid. Never select using a test label.
    return min(CANDIDATES, key=lambda weight: (_nll(frame, weight, steps),
                                              CANDIDATES.index(weight)))


def evaluate_blend(frames: dict, *, horizon: int, train_end: datetime,
                   test_start: datetime) -> dict:
    """Offline validation/test comparison, distinct direction/size/timing labels.

    frames: stream key -> (five-minute candle DataFrame, archive features).
    The model's own outcome and promotion gates are untouched.
    """
    import pandas as pd

    if horizon not in (5, 15, 60) or not isinstance(frames, dict):
        raise ValueError("unsupported research horizon or source input")
    train_end, test_start = _utc(train_end), _utc(test_start)
    if train_end >= test_start - timedelta(minutes=horizon):
        raise ValueError("validation must precede test by a full horizon")
    steps = horizon // 5
    valid = []
    unusable = []
    for key, item in sorted(frames.items(), key=lambda x: str(x[0])):
        if not isinstance(item, (tuple, list)) or not item:
            unusable.append(str(key))
            continue
        try:
            part = _source(item[0], steps=steps, train_end=train_end)
        except (KeyError, ValueError, TypeError, AttributeError):
            part = None
        if part is None or part.empty:
            unusable.append(str(key))
            continue
        part = part.loc[part.index >= train_end].copy()
        if part.empty:
            unusable.append(str(key))
            continue
        part["source"] = str(key)
        valid.append(part)
    basic = {
        "schema": "rival-volatility-blend-offline-diagnostic-v1",
        "purpose": "Research only, no model promotion or executable trading evidence",
        "windows": {"24h": 288, "7d": 2016, "30d": 8640},
        "selection": "fixed grid chosen by validation Gaussian log score only",
        "label": "source-exact contiguous 5-minute closing prices",
        "horizon_minutes": horizon,
        "usable_sources": len(valid),
        "unusable_sources": unusable[:25],
        "not_independent_samples": True,
        "qualified_for_live_use": False,
        "proven_after_cost_edge": False,
        "original_uploaded_parameters_used": False,
    }
    if not valid:
        return {**basic, "status": "missing_contiguous_30d_source_history"}
    data = pd.concat(valid, axis=0).sort_index()
    # Purge validation examples that could see a future outcome inside test.
    validation = data.loc[(data.index >= train_end)
                          & (data.index < test_start - timedelta(minutes=horizon))]
    test = data.loc[data.index >= test_start]
    basic["validation"] = {"rows": len(validation), "days": _days(validation)}
    basic["test"] = {"rows": len(test), "days": _days(test)}
    if (min(len(validation), len(test)) < MIN_WINDOW_ROWS
            or min(_days(validation), _days(test)) < MIN_WINDOW_DAYS):
        return {**basic, "status": "insufficient_contiguous_validation_or_test_history"}
    best = _select(validation, steps)
    baseline = CANDIDATES[0]
    test_baseline = _metric(test, baseline, steps)
    test_best = _metric(test, best, steps)
    time_to = test.loc[test["first_cross_bars"] > 0, "first_cross_bars"]
    ret = test["realized"]
    basic["separate_targets"] = {
        "direction": {
            "outcome": "terminal close > issuance close",
            "graded_targets": len(test),
            "up_fraction": round(float((ret > 0).mean()), 5),
            "direction_model_tested": False,
            "no_information_brier": 0.25,
        },
        "move_size": {
            "outcome": "absolute terminal log return",
            "mean_absolute_log_return": round(float(ret.abs().mean()), 8),
            "selected_blend_interval_coverage": test_best["central_90_interval_coverage"],
            "baseline_24h_interval_coverage": test_baseline["central_90_interval_coverage"],
        },
        "timing": {
            "outcome": "first future 5m CLOSE exceeding 24h one-sigma horizon threshold",
            "hit_targets": len(time_to),
            "hit_fraction": round(len(time_to) / len(test), 5),
            "median_first_hit_bars": (float(time_to.median()) if len(time_to) else None),
            "timing_model_tested": False,
            "intrabar_first_passage_inferred": False,
        },
    }
    regimes = {}
    for regime in ("calm", "normal", "turbulent"):
        v = validation.loc[validation["regime"] == regime]
        t = test.loc[test["regime"] == regime]
        if min(len(v), len(t)) < MIN_REGIME_ROWS or _days(v) < 4 or _days(t) < 4:
            regimes[regime] = {"status": "insufficient_distinct_regime_evidence",
                               "validation_rows": len(v), "test_rows": len(t)}
            continue
        selected = _select(v, steps)
        regimes[regime] = {
            "status": "diagnostic_only",
            "validation_rows": len(v),
            "test_rows": len(t),
            "weights_24h_7d_30d": list(selected),
            "holdout_nll_gain_vs_24h": round(
                _nll(t, baseline, steps) - _nll(t, selected, steps), 8),
        }
    return {
        **basic, "status": "evaluated_offline_not_promotable",
        "selected_weights_24h_7d_30d": list(best),
        "validation_nll_selected": round(_nll(validation, best, steps), 8),
        "validation_nll_24h": round(_nll(validation, baseline, steps), 8),
        "holdout": {
            "selected": test_best, "baseline_24h": test_baseline,
            "nll_gain_vs_24h": round(test_baseline["nll"] - test_best["nll"], 8),
        },
        "regime_research": regimes,
        "separate_targets": basic["separate_targets"],
        "limitations": (
            "Overlapping horizons/cross-venue samples are correlated; "
            "weight selection uses multiple candidates. This diagnostic "
            "is neither calibrated directional skill nor trade profitability."
        ),
    }
