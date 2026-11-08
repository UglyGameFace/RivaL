"""Offline trainer for the regime_v2 challenger (trending-subset logistic).

Spec rival-regime-v2-spec-20261108, decisions D1-D5. The archive
logistic on the 32-feature contract, trained ONLY on trending candles
(Kaufman efficiency ratio ER > 0.30 over the last 20 closed 5-minute
bars -- the frozen regime_v1 gate). Same frames/labels/splits/gates/
provenance/fit as archive_v1, so the comparison "archive_v1 trained on
all rows vs regime_v2 trained on trending rows" isolates exactly one
variable: the training rows. No serving path, no Postgres writes, no
promotion-table writes anywhere in this module.

The trending mask is causal: the ER at row i uses closes i-20 .. i
only (bars closed at or before i), aligned to the same bar-close index
the labels use. Rows with no ER (first 20 bars, gaps breaking 300s
contiguity, non-finite/non-positive closes) are excluded, never
imputed. The mask is computed per stream from the candles DataFrame's
close column -- no new data source.

Floors (D1, spec decision): per (asset, horizon) run, trending
labelled rows must satisfy train >= 2000, val >= 300, test >= 300,
else TrainingDataUnavailable -> report only, no artifact. The census
(per-venue trending share, per-split trending counts) is published in
every report, and attached to the TrainingDataUnavailable raised by a
floor failure so the weekly run's blocked report still carries it.

The offline baseline is the trending-subset base rate, not the
all-rows base rate: a v2 offline gain of 0.005 is not comparable to
an archive_v1 offline gain of 0.005. The Route B paired gain is the
comparable number.

The artifact differs from archive_v1's by identity, not by shape:
SCHEMA "rival-regime2-model-v1", FAMILY "regime_v2", MODEL_VERSION
"regime-offline-v1". Its feature digest pins the subset descriptor
alongside the contract, so any future change to the subset definition
is a new family, not a silent retrain of this one.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.perpetuals.pooled_features import ASSETS
from parlay_bot.perpetuals.regime_gate import ER_THRESHOLD, ER_WINDOW
from parlay_bot.research.archive_features import (
    FEATURE_CONTRACT,
    FEATURE_SOURCES,
    FEATURES,
    GROUPS,
)
from parlay_bot.research.archive_features import (
    FEATURE_DIGEST as ARCHIVE_FEATURE_DIGEST,
)
from parlay_bot.research.archive_features import (
    MODEL_VERSION as ARCHIVE_MODEL_VERSION,
)
from parlay_bot.research.archive_training import (
    TrainingDataUnavailable,
    build_labels,
    census_summary,
    chronological_splits,
    load_binance_frame,
    load_frames_csv,
    load_frames_manifests,
)

SCHEMA = "rival-regime2-model-v1"
FAMILY = "regime_v2"
MODEL_VERSION = "regime-offline-v1"
HORIZONS = (5, 15, 60)
VENUES = ("binance", "bitget", "okx", "gate")
GATES = {
    "min_validation_gain": 0.005,
    "min_test_gain": 0.005,
    "min_test_daily_positive": 0.55,
    "max_test_ece": 0.03,
}
MIN_CONTEXT_ABSENT_GAIN = 0.005
HOLDOUT_DAYS = 28
SERVE_CLAMP = (0.02, 0.98)
# Batch gradient descent, mirroring the archive logistic constants.
LEARNING_RATE = 0.5
L2 = 1e-4
EPOCHS = 300
# D1 floors on trending labelled rows per (asset, horizon) run.
MIN_TRENDING_TRAIN = 2000
MIN_TRENDING_VAL = 300
MIN_TRENDING_TEST = 300
SUBSET_DESCRIPTOR = "efficiency-ratio-20|er>0.30|trending-rows-only"
TRAINING_SUBSET = {
    "method": "efficiency-ratio-20",
    "gate": "er>0.30",
    "per_stream": True,
    "causal": True,
    "descriptor": SUBSET_DESCRIPTOR,
}
# The feature digest pins the subset descriptor alongside the contract:
# the same 32 features, a different training population. Deliberately
# not ARCHIVE_FEATURE_DIGEST, so a v2 artifact can never be mistaken
# for an archive_v1 artifact (or vice versa) by any digest check.
FEATURE_DIGEST = hashlib.sha256(
    json.dumps([FEATURE_CONTRACT, *FEATURES, SUBSET_DESCRIPTOR],
               separators=(",", ":")).encode()
).hexdigest()
_SHA64 = __import__("re").compile(r"[0-9a-f]{64}")

# The archive digest is the contract this module reuses: importing it
# here keeps the relationship explicit (same features, different
# subset) and lets tests assert the two digests differ by exactly the
# subset descriptor.
assert ARCHIVE_FEATURE_DIGEST != FEATURE_DIGEST
assert ARCHIVE_MODEL_VERSION == "archive-offline-v1"


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def trending_mask(closes):
    """Per-stream trending mask over a closes Series (bar-close index).

    True at row i when the Kaufman efficiency ratio over closes
    i-20 .. i strictly exceeds 0.30 (the frozen regime_v1 gate).
    False for the first 20 rows, for any window broken by a gap in the
    5-minute spacing, and for any window holding a non-finite or
    non-positive close. Flat windows evaluate to ER 0.0, hence False.
    The mask is causal: row i reads closes i-20 .. i only.
    """
    import pandas as pd

    c = closes.astype(float)
    moves = c.diff().abs()
    window_sum = moves.rolling(ER_WINDOW).sum()
    net = (c - c.shift(ER_WINDOW)).abs()
    er = net / window_sum
    er = er.where(window_sum > 0, 0.0)
    idx = c.index.to_series()
    gaps = idx.diff() != pd.Timedelta(minutes=5)
    # 20 internal diffs in a 21-close window (the diff into the window's
    # first bar belongs to the previous window, not this one).
    bad_gaps = gaps.rolling(ER_WINDOW).sum()
    valid = c.gt(0) & c.notna()
    valid_n = valid.rolling(ER_WINDOW + 1).sum()
    out = (er > ER_THRESHOLD) & (bad_gaps == 0) & (valid_n == ER_WINDOW + 1)
    return out.fillna(False)


def _prepare_parts(frames, *, horizon):
    """Attach labels + trending mask per stream.

    Returns (parts, coverage_frames, venue_census): parts are the
    feature frames with "y", "stream" and "trending" columns;
    venue_census is per-venue total bars / trending bars / share.
    """
    parts, coverage_frames = [], {}
    venue_census = {}
    for key, (k, f) in frames.items():
        mask = trending_mask(k["close"])
        venue = str(key).split("|", 1)[0]
        slot = venue_census.setdefault(venue, {"bars": 0, "trending_bars": 0})
        slot["bars"] += len(k)
        slot["trending_bars"] += int(mask.sum())
        part = f.copy()
        part["y"] = build_labels(k, horizon)
        part["stream"] = str(key)
        part["trending"] = (
            mask.reindex(part.index).fillna(False).to_numpy(dtype=bool)
        )
        parts.append(part)
        coverage_frames[key] = (k, f)
    for slot in venue_census.values():
        slot["trending_share"] = (
            round(slot["trending_bars"] / slot["bars"], 6) if slot["bars"] else 0.0
        )
    return parts, coverage_frames, venue_census


def _census_splits(data, train_mask, val_mask, test_mask) -> dict:
    """Per-split labelled / trending-labelled rows with share."""
    out = {}
    for name, mask in (("train", train_mask), ("validation", val_mask),
                       ("test", test_mask)):
        part = data[mask]
        labelled = len(part)
        trending = int(part["trending"].sum()) if labelled else 0
        out[name] = {
            "labelled": labelled,
            "trending_labelled": trending,
            "trending_share": round(trending / labelled, 6) if labelled else 0.0,
        }
    return out


def _anchor_features(asset: str) -> tuple:
    """Anchor feature indices: present groups; context groups may be
    missing. A BTC stream is not its own BTC lead: btc_ret_1/12 and
    rel_ret_12 are NaN by pooled design, so they are not anchors for
    BTC. One definition, used by train_run and trending_census alike --
    the census must measure what the trainer measures."""
    anchor = tuple(
        GROUPS["candle"] + GROUPS["multi_horizon"] + GROUPS["calendar"] + GROUPS["cross_asset"]
    )
    if asset == "BTC":
        anchor = tuple(
            i for i in anchor
            if FEATURES[i] not in ("btc_ret_1", "btc_ret_12", "rel_ret_12")
        )
    return anchor


def trending_census(frames, *, asset, train_end=None, test_start=None, horizon: int = 5) -> dict:
    """Standalone trending census (D1): same mask code, no fitting.

    Always reports per-venue bars / trending bars / share. With
    train_end + test_start it also reports per-split labelled /
    trending-labelled rows with share, on the same chronological
    splits train_run uses.
    """
    import pandas as pd

    parts, _, venue_census = _prepare_parts(frames, horizon=horizon)
    out = {"per_venue": venue_census, "splits": None}
    if train_end is None or test_start is None:
        return out
    if not parts:
        raise TrainingDataUnavailable("no frames to census")
    data = pd.concat(parts).sort_index()
    anchor = _anchor_features(asset)
    data = data.dropna(subset=["y", *[FEATURES[i] for i in anchor]])
    train_mask, val_mask, test_mask = chronological_splits(
        data.index, train_end=train_end, test_start=test_start, horizon=horizon
    )
    out["splits"] = _census_splits(data, train_mask, val_mask, test_mask)
    return out


def _standardize_fit(x):
    """Train-only standardiser: mean/std per feature, NaN-aware."""
    import warnings

    import numpy as np

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = np.nanmean(x, axis=0)
        std = np.nanstd(x, axis=0)
    std = np.where(np.isfinite(std) & (std > 1e-9), std, 1.0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    return mean, std


def _standardize_apply(x, mean, std):
    import numpy as np

    z = (x - mean) / std
    z = np.where(np.isfinite(z), z, 0.0)  # missing group -> group mean
    return np.clip(z, -5, 5)


def _fit_logistic(x, y, *, epochs: int = EPOCHS, rate: float = LEARNING_RATE, l2: float = L2):
    """Full-batch gradient descent logistic regression (deterministic).

    x is already standardised (missing -> 0). Weights: bias + 32.
    Mirrors archive_training._fit_logistic exactly: same hyperparams,
    same fit, only the rows differ.
    """
    import numpy as np

    n, width = x.shape
    base = float(np.mean(y))
    weights = np.zeros(width + 1)
    weights[0] = math.log(base / (1 - base)) if 0 < base < 1 else 0.0
    design = np.hstack([np.ones((n, 1)), x])
    for _ in range(epochs):
        p = 1 / (1 + np.exp(-np.clip(design @ weights, -30, 30)))
        error = p - y
        grad = design.T @ error / n
        grad[1:] += l2 * weights[1:]
        weights -= rate * grad
        if not np.all(np.isfinite(weights)):
            raise ValueError("non-finite logistic weights")
    return weights


def _raw_probabilities(weights, x) -> object:
    import numpy as np

    design = np.hstack([np.ones((len(x), 1)), x])
    return 1 / (1 + np.exp(-np.clip(design @ weights, -30, 30)))


def _platt_apply(a: float, b: float, p):
    import numpy as np

    logit = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    out = 1 / (1 + np.exp(-np.clip(a * logit + b, -30, 30)))
    return np.clip(out, *SERVE_CLAMP)


def _metrics(p, y, base: float, index) -> dict:
    from parlay_bot.research.pooled_training import metrics

    return metrics(p, y, base, index)


def train_run(
    frames: dict,
    *,
    asset: str,
    horizon: int,
    train_end,
    test_start,
    trained_through,
    provenance: dict,
    code_version: str,
    months_requested=None,
) -> dict:
    """Train one (asset, horizon) regime_v2 artifact and gate it.

    frames: stream key -> (candles DataFrame, features DataFrame).
    Returns {"publish_kind": "model"|"report", "artifact": doc|None,
    "report": {...}}. A failed gate publishes a report only. A D1
    floor failure raises TrainingDataUnavailable with the census
    attached (``exc.census``) so the weekly run's blocked report
    still carries it.
    """
    import pandas as pd

    from parlay_bot.perpetuals.flow_challenger_v3 import (
        calibration_gate,
        fit_platt,
    )
    from parlay_bot.research.pooled_training import frame_coverage

    if asset not in ASSETS:
        raise ValueError(f"unsupported asset: {asset!r}")
    if horizon not in HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon!r}")
    anchor = _anchor_features(asset)
    parts, coverage_frames, venue_census = _prepare_parts(
        frames, horizon=horizon)
    if not parts:
        # No frames at all: the census is per-venue empty, and it still
        # rides on the exception so the blocked report measures the
        # shortfall instead of merely naming it.
        exc = TrainingDataUnavailable("no frames to train on")
        exc.census = {"per_venue": venue_census, "splits": None}
        raise exc
    data = pd.concat(parts).sort_index()
    # Anchor groups must be present; context groups may be missing.
    data = data.dropna(subset=["y", *[FEATURES[i] for i in anchor]])
    train_mask, val_mask, test_mask = chronological_splits(
        data.index, train_end=train_end, test_start=test_start, horizon=horizon
    )
    # The census is computed BEFORE any size check, so every blocked
    # path below (too few labelled rows, a split too small, a floor
    # failure) carries it on the exception.
    split_census = _census_splits(data, train_mask, val_mask, test_mask)
    census = {"per_venue": venue_census, "splits": split_census}
    if len(data) < 300:
        exc = TrainingDataUnavailable(f"not enough labelled rows: {len(data)}")
        exc.census = census
        raise exc
    train, val, test = data[train_mask], data[val_mask], data[test_mask]
    if len(train) < 300 or len(val) < 100 or len(test) < 100:
        exc = TrainingDataUnavailable(
            f"split too small: train={len(train)} val={len(val)} test={len(test)}"
        )
        exc.census = census
        raise exc
    # D1 floors: trending labelled rows only. A failure is a
    # report-only run, never a reason to lower the floor for an asset.
    trending_counts = {
        name: split_census[name]["trending_labelled"]
        for name in ("train", "validation", "test")
    }
    if (trending_counts["train"] < MIN_TRENDING_TRAIN
            or trending_counts["validation"] < MIN_TRENDING_VAL
            or trending_counts["test"] < MIN_TRENDING_TEST):
        exc = TrainingDataUnavailable(
            f"trending split too small: train={trending_counts['train']} "
            f"val={trending_counts['validation']} "
            f"test={trending_counts['test']} "
            f"(floors {MIN_TRENDING_TRAIN}/{MIN_TRENDING_VAL}/{MIN_TRENDING_TEST})"
        )
        exc.census = census
        raise exc
    # The one variable that changes vs archive_v1: the rows.
    train_t = train[train["trending"]]
    val_t = val[val["trending"]]
    test_t = test[test["trending"]]
    cols = list(FEATURES)
    mean, std = _standardize_fit(train_t[cols].to_numpy(dtype=float))
    x_train = _standardize_apply(train_t[cols].to_numpy(dtype=float), mean, std)
    x_val = _standardize_apply(val_t[cols].to_numpy(dtype=float), mean, std)
    x_test = _standardize_apply(test_t[cols].to_numpy(dtype=float), mean, std)
    weights = _fit_logistic(x_train, train_t["y"].to_numpy(dtype=float))
    # Offline baseline is the TRENDING-subset base rate.
    base = float(train_t["y"].mean())
    p_val_raw = _raw_probabilities(weights, x_val)
    p_test_raw = _raw_probabilities(weights, x_test)
    # Calibration: Platt fitted on the validation window ONLY, gated
    # on the disjoint test window. Never inherit another family's (a, b).
    fit_rows = [(float(p), int(y)) for p, y in zip(p_val_raw, val_t["y"].to_numpy(dtype=float))]
    eval_rows = [(float(p), int(y)) for p, y in zip(p_test_raw, test_t["y"].to_numpy(dtype=float))]
    platt = fit_platt(fit_rows, eval_rows)
    calibration_ok = platt.get("status") == "evaluated"
    if calibration_ok:
        gate = calibration_gate({"calibration": platt}, eval_rows)
        calibration_ok = bool(gate.get("passes"))
        platt["gate_ece"] = gate.get("ece")
        platt["gate_top_bucket_hit_rate"] = gate.get("top_bucket_hit_rate")
    if calibration_ok:
        p_test = _platt_apply(float(platt["a"]), float(platt["b"]), p_test_raw)
        p_val = _platt_apply(float(platt["a"]), float(platt["b"]), p_val_raw)
    else:
        # Uncalibrated probabilities are never published as a model.
        p_test, p_val = p_test_raw, p_val_raw
    val_metrics = _metrics(p_val, val_t["y"].to_numpy(dtype=float), base, val_t.index)
    test_metrics = _metrics(p_test, test_t["y"].to_numpy(dtype=float), base, test_t.index)
    # Track C: context-absent evaluation -- the Binance tape group
    # flagged off (standardised inputs forced to the group mean).
    # Must pass the same 0.005 bar, or the artifact is not
    # serving-capable on the production host.
    x_test_absent = x_test.copy()
    x_test_absent[:, list(GROUPS["binance_tape"])] = 0.0
    p_absent_raw = _raw_probabilities(weights, x_test_absent)
    p_absent = (
        _platt_apply(float(platt["a"]), float(platt["b"]), p_absent_raw)
        if calibration_ok
        else p_absent_raw
    )
    absent_metrics = _metrics(p_absent, test_t["y"].to_numpy(dtype=float), base, test_t.index)
    context_absent_ok = absent_metrics["brier_gain"] > MIN_CONTEXT_ABSENT_GAIN
    # 28-day venue holdout, per stream, on the trending test slice.
    holdout = venue_holdout(data, test_t, weights, mean, std, base,
                            platt if calibration_ok else None)
    coverage = frame_coverage(coverage_frames, months_requested=months_requested)
    coverage["months_in_train"] = sorted(set(train_t.index.strftime("%Y-%m")))
    coverage["days_in_train"] = len(set(train_t.index.normalize()))
    gates_passed = bool(
        val_metrics["brier_gain"] > GATES["min_validation_gain"]
        and test_metrics["brier_gain"] > GATES["min_test_gain"]
        and test_metrics["daily_positive_fraction"] >= GATES["min_test_daily_positive"]
        and test_metrics["ece"] <= GATES["max_test_ece"]
        and calibration_ok
        and context_absent_ok
    )
    reasons = []
    if val_metrics["brier_gain"] <= GATES["min_validation_gain"]:
        reasons.append("validation_brier_gain<=0.005")
    if test_metrics["brier_gain"] <= GATES["min_test_gain"]:
        reasons.append("test_brier_gain<=0.005")
    if test_metrics["daily_positive_fraction"] < GATES["min_test_daily_positive"]:
        reasons.append("test_daily_positive_fraction<0.55")
    if test_metrics["ece"] > GATES["max_test_ece"]:
        reasons.append("test_ece>0.03")
    if not calibration_ok:
        reasons.append("calibration_gate_failed")
    if not context_absent_ok:
        reasons.append("context_absent_gain<=0.005")
    evaluation = {
        "validation": val_metrics,
        "test": test_metrics,
        "context_absent_test": absent_metrics,
        "venue_holdout_28d": holdout,
        "train_rows": len(train_t),
        "train_before": str(train_end),
        "test_from": str(test_start),
    }
    report = {
        "schema": SCHEMA,
        "family": FAMILY,
        "asset": asset,
        "horizon_minutes": horizon,
        "feature_contract": FEATURE_CONTRACT,
        "gates": GATES,
        "gates_passed": gates_passed,
        "gate_failures": reasons,
        "evaluation": evaluation,
        "coverage": coverage,
        "trending_census": census,
        "training_subset": TRAINING_SUBSET,
        "provenance": provenance,
        "code_version": code_version,
        "evaluation_status": "held-out evaluated",
        "serving_capable_without_binance_context": context_absent_ok,
        "serve_clamp": list(SERVE_CLAMP),
    }
    if not gates_passed:
        return {"publish_kind": "report", "artifact": None, "report": report}
    doc = {
        **report,
        "model_version": MODEL_VERSION,
        "features": cols,
        "feature_sources": {name: list(FEATURE_SOURCES[name]) for name in FEATURES},
        "feature_digest": FEATURE_DIGEST,
        "weights": [float(w) for w in weights],
        "standardizer": {"mean": [float(v) for v in mean], "std": [float(v) for v in std]},
        "calibration": {
            k: platt.get(k)
            for k in (
                "method",
                "status",
                "a",
                "b",
                "ece",
                "fit_rows",
                "eval_rows",
                "top_bucket_hit_rate",
                "top_bucket_n",
                "top_bucket_mean_confidence",
                "gate_ece",
                "gate_top_bucket_hit_rate",
            )
        },
        "trained_from": str(data.index.min()),
        "trained_through": str(trained_through),
        "training_rows": len(train_t),
        "created_at": datetime.now(UTC).isoformat(),
        "weights_sha256": hashlib.sha256(
            json.dumps([float(w) for w in weights], separators=(",", ":")).encode()
        ).hexdigest(),
    }
    return {"publish_kind": "model", "artifact": doc, "report": report}


def venue_holdout(data, test, weights, mean, std, base: float, platt: dict | None) -> dict:
    """Per-stream Brier gain on the last 28 days of the test slice."""
    import numpy as np
    import pandas as pd

    cols = list(FEATURES)
    out = {}
    if test.empty:
        return out
    start = test.index.max().normalize() - pd.Timedelta(days=HOLDOUT_DAYS)
    for stream, part in test[test.index >= start].groupby("stream"):
        x = _standardize_apply(part[cols].to_numpy(dtype=float), mean, std)
        p = _raw_probabilities(weights, x)
        if platt is not None:
            p = _platt_apply(float(platt["a"]), float(platt["b"]), p)
        y = part["y"].to_numpy(dtype=float)
        days = (
            pd.Series(((base - y) ** 2) - ((p - y) ** 2), index=part.index)
            .groupby(part.index.date)
            .mean()
        )
        out[str(stream)] = {
            "rows": len(part),
            "days": len(days),
            "brier_gain": round(float(np.mean((base - y) ** 2) - np.mean((p - y) ** 2)), 6),
            "daily_positive_fraction": round(float((days > 0).mean()), 3) if len(days) else None,
        }
    return out


def validate(doc: dict) -> dict:
    """Fail closed on any artifact that must not enter the import path."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not a regime_v2 model document")
    if doc.get("family") != FAMILY or doc.get("model_version") != MODEL_VERSION:
        raise ValueError("regime_v2 model identity mismatch")
    if doc.get("feature_contract") != FEATURE_CONTRACT:
        raise ValueError("regime_v2 model feature contract mismatch")
    if tuple(doc.get("features", ())) != FEATURES:
        raise ValueError("regime_v2 model built for different inputs")
    if doc.get("feature_digest") != FEATURE_DIGEST:
        raise ValueError("regime_v2 model feature digest mismatch")
    if doc.get("training_subset", {}).get("descriptor") != SUBSET_DESCRIPTOR:
        raise ValueError("regime_v2 model trained on a different subset")
    weights = doc.get("weights")
    if not isinstance(weights, list) or len(weights) != len(FEATURES) + 1:
        raise ValueError("regime_v2 model weights have the wrong width")
    if not all(isinstance(w, (int, float)) and math.isfinite(w) for w in weights):
        raise ValueError("regime_v2 model weights are not finite")
    calibration = doc.get("calibration") or {}
    if calibration.get("status") != "evaluated":
        raise ValueError("regime_v2 model calibration did not pass its gate")
    if not all(
        isinstance(calibration.get(k), (int, float)) and math.isfinite(calibration[k])
        for k in ("a", "b")
    ):
        raise ValueError("regime_v2 model calibration parameters are invalid")
    provenance = doc.get("provenance") or {}
    provider = provenance.get("provider_sha256_by_month")
    if not isinstance(provider, dict) or not provider:
        raise ValueError("regime_v2 model provenance lacks provider hashes")
    for month, sha in provider.items():
        if not isinstance(sha, str) or not _SHA64.fullmatch(sha):
            raise ValueError(f"regime_v2 model provider hash for {month} is not a SHA-256")
    normalized = provenance.get("normalized_sha256_by_month")
    if isinstance(normalized, dict):
        # P1-4 lesson: a normalized NDJSON hash must never stand in as a
        # provider hash. Any month whose provider hash equals its normalized
        # hash is refused.
        for month, sha in normalized.items():
            if provider.get(month) == sha:
                raise ValueError(f"regime_v2 model provider hash for {month} is the normalized hash")
    if not isinstance(provenance.get("months_without_provider_hash"), list):
        raise TypeError("regime_v2 model provenance lacks months_without_provider_hash")
    if doc.get("gates_passed") is not True:
        raise ValueError("regime_v2 model did not pass its offline gates")
    return doc


def usable(doc: dict, *, now: datetime | None = None) -> bool:
    """Only held-out-evaluated, gate-passing, calibrated artifacts.

    A document labelled "backtest-only, shipped model unevaluated" is
    never usable: its evaluation describes weights that were refit after
    scoring (the pooled precedent, pooled_model.py:130-145).
    """
    if doc.get("evaluation_status") == "backtest-only, shipped model unevaluated":
        return False
    try:
        validate(doc)
        trained = datetime.fromisoformat(str(doc["trained_through"]))
        trained = trained if trained.utcoffset() else trained.replace(tzinfo=UTC)
        return trained < (now or datetime.now(UTC))
    except (KeyError, ValueError, TypeError):
        return False


def probability(doc: dict, vector) -> float:
    """Serve-time probability for a 32-value vector (NaN = group absent).

    Standardised with the artifact's train-only standardiser (missing
    groups contribute the group mean, i.e. standardised 0), Platt
    calibrated, clamped to [0.02, 0.98]."""
    import numpy as np

    validate(doc)
    if len(vector) != len(FEATURES):
        raise ValueError("wrong regime_v2 feature count")
    mean = np.array(doc["standardizer"]["mean"], dtype=float)
    std = np.array(doc["standardizer"]["std"], dtype=float)
    values = np.array(vector, dtype=float)
    x = _standardize_apply(values, mean, std)
    raw = _sigmoid(float(np.hstack([[1.0], x]) @ np.array(doc["weights"], dtype=float)))
    calibration = doc["calibration"]
    calibrated = _sigmoid(
        float(calibration["a"])
        * math.log(max(1e-6, min(1 - 1e-6, raw)) / (1 - max(1e-6, min(1 - 1e-6, raw))))
        + float(calibration["b"])
    )
    return float(min(SERVE_CLAMP[1], max(SERVE_CLAMP[0], calibrated)))


def artifact_identity(doc: dict) -> dict:
    """Pinning identity for the D14 selected() branch: feature digest
    equal, trained-through / graded-count monotone. Weights SHA is
    recomputed from the stored weights, never trusted from a stored
    column."""
    validate(doc)
    return {
        "feature_digest": doc["feature_digest"],
        "weights_sha256": hashlib.sha256(
            json.dumps(doc["weights"], separators=(",", ":")).encode()
        ).hexdigest(),
        "trained_through": doc["trained_through"],
        "graded_count": int(doc.get("graded_count", 0)),
    }


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def _publish(kind: str, path: Path) -> dict:
    """Publish one artifact/report through the existing Drive plumbing
    (source_native_offline pattern: ensure_research_output, kind model or
    report, manifest cross-check at bot import). No Postgres writes."""
    from parlay_bot.config import Settings
    from parlay_bot.research.publish import _artifact, _drive

    drive = _drive(Settings())
    try:
        item = _artifact(path, 1)
        restored = await drive.ensure_research_output(
            artifact=item, batch_id=f"regime-v2-{item.sha256[:24]}", kind=kind
        )
        return {
            "kind": kind,
            "file_id": restored.file_id,
            "sha256": item.sha256,
            "live_predictions_modified": False,
        }
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train the regime_v2 offline logistic (trending-subset only).")
    parser.add_argument("--asset", required=True, choices=list(ASSETS))
    parser.add_argument("--horizon", required=True, type=int, choices=list(HORIZONS))
    parser.add_argument("--frames-dir", type=Path, help="CSV frames directory (dry run / fixtures)")
    parser.add_argument("--manifests-dir", type=Path, help="Restored verified manifests (GHA path)")
    parser.add_argument(
        "--census", type=Path, help="drive-source-census.json; the job measures first"
    )
    parser.add_argument(
        "--provenance", type=Path, help="JSON with provider_sha256_by_month for CSV frames"
    )
    parser.add_argument(
        "--train-end", required=True, help="Validation start, ISO (train < this, purged)"
    )
    parser.add_argument(
        "--test-start", required=True, help="Test start, ISO (validation < this, purged)"
    )
    parser.add_argument(
        "--trained-through",
        help="Last bar time the artifact was trained through, ISO",
    )
    parser.add_argument(
        "--binance-cache", type=Path, help="Binance Vision ZIP cache dir (pooled path)"
    )
    parser.add_argument(
        "--binance-last-month", default=None, help="Last complete Binance month, YYYY-MM"
    )
    parser.add_argument(
        "--binance-through-day",
        default=None,
        help="Binance daily files through this date, YYYY-MM-DD",
    )
    parser.add_argument("--out", type=Path, help="Output directory (training runs)")
    parser.add_argument("--code-version", default=None)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish kind=model (pass) or kind=report (fail) to Drive",
    )
    parser.add_argument(
        "--trending-census",
        action="store_true",
        help="Print the D1 trending census and exit without fitting",
    )
    args = parser.parse_args(argv)
    train_end = datetime.fromisoformat(args.train_end)
    test_start = datetime.fromisoformat(args.test_start)
    if train_end.tzinfo is None or test_start.tzinfo is None:
        raise ValueError("train-end and test-start must be timezone-aware ISO")
    census = census_summary(args.census) if args.census else None
    if args.manifests_dir is not None:
        frames, provenance = load_frames_manifests(args.manifests_dir, target_asset=args.asset)
    elif args.frames_dir is not None:
        frames = load_frames_csv(args.frames_dir)
        provenance = (
            json.loads(args.provenance.read_text(encoding="utf-8"))
            if args.provenance
            else {
                "provider_sha256_by_month": {},
                "months_without_provider_hash": [],
                "manifest_sha256_by_month": {},
            }
        )
    else:
        raise ValueError("one of --frames-dir or --manifests-dir is required")
    if args.trending_census:
        # D1 standalone census: same mask code, no fitting.
        print(json.dumps(
            trending_census(frames, asset=args.asset,
                            train_end=train_end, test_start=test_start,
                            horizon=args.horizon),
            indent=2, sort_keys=True))
        return 0
    if args.trained_through is None or args.out is None:
        raise ValueError("--trained-through and --out are required to train")
    if args.binance_cache is not None:
        from datetime import date as _date

        if not args.binance_last_month:
            raise ValueError("--binance-last-month is required with --binance-cache")
        through = (
            _date.fromisoformat(args.binance_through_day) if args.binance_through_day else None
        )
        btc_k, btc_f, btc_cov = asyncio.run(
            load_binance_frame(
                args.asset, args.binance_cache, args.binance_last_month, through_day=through
            )
        )
        frames[f"binance|{args.asset}USDT|USDT"] = (btc_k, btc_f)
        # Binance ZIP SHAs are verified against the provider .CHECKSUM at
        # fetch time but not retained per month by that loader.
        for month in (btc_cov.get("klines") or {}).get("months_loaded", []):
            provenance.setdefault("months_without_provider_hash", []).append(f"binance:{month}")
        provenance["binance_zip_sha256_verified_at_fetch"] = True
        provenance["binance_coverage"] = btc_cov
    result = train_run(
        frames,
        asset=args.asset,
        horizon=args.horizon,
        train_end=train_end,
        test_start=test_start,
        trained_through=args.trained_through,
        provenance=provenance,
        code_version=args.code_version or _git_sha(),
        months_requested=None,
    )
    report = dict(result["report"])
    if census is not None:
        report["census"] = census
    out_dir = args.out
    report_path = _write_json(
        out_dir / f"regime-v2-{args.asset}-{args.horizon}m-report.json", report
    )
    published = {}
    if result["publish_kind"] == "model":
        doc = validate(result["artifact"])
        artifact_path = _write_json(
            out_dir / f"regime-v2-{args.asset}-{args.horizon}m-model.json", doc
        )
        print(f"regime-v2 {args.asset} {args.horizon}m: gates PASSED, artifact {artifact_path}")
        if args.publish:
            published = asyncio.run(_publish("model", artifact_path))
    else:
        print(
            f"regime-v2 {args.asset} {args.horizon}m: gates FAILED "
            f"({', '.join(result['report']['gate_failures'])}); report only"
        )
        if args.publish:
            published = asyncio.run(_publish("report", report_path))
    if args.publish and not published:
        raise RuntimeError("publish requested but nothing was published")
    if published:
        print(f"published kind={published['kind']} file_id={published['file_id']}")
    return 0 if result["publish_kind"] == "model" else 1


if __name__ == "__main__":
    raise SystemExit(main())
