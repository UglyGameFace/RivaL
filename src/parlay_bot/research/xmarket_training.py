"""Offline per-asset/per-horizon logistic, using VolCal's fitting and gates.

All input rows are complete and finite before the shared standardizer runs.
Missing source history produces a measured report, never imputed inputs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.perpetuals.markets import ASSETS, asset_for_contract
from parlay_bot.research.archive_training import (
    _fit_logistic,
    _git_sha,
    _metrics,
    _platt_apply,
    _raw_probabilities,
    _standardize_apply,
    _standardize_fit,
    _write_json,
    build_labels,
    census_summary,
    chronological_splits,
    load_frames_csv,
    load_frames_manifests,
)
from parlay_bot.research.xmarket_features import (
    BASE_FEATURES,
    DISPERSION_FEATURES,
    FEATURE_CONTRACT,
    MARKET_FEATURES,
    MODEL_VERSION,
    PRICE_SEMANTICS,
    feature_digest,
    feature_names,
    frame_features,
    sampled_bars,
)

SCHEMA = "rival-xmarket-model-v1"
FAMILY = "xmarket_v1"
HORIZONS = (5, 15, 60)
VENUES = ("binance", "bitget", "okx", "gate")
GATES = {
    "min_validation_gain": 0.005,
    "min_test_gain": 0.005,
    "min_test_daily_positive": 0.55,
    "max_test_ece": 0.03,
}
HOLDOUT_DAYS = 28
SERVE_CLAMP = (0.02, 0.98)
_SHA64 = re.compile(r"[0-9a-f]{64}")


def _supplement_provenance(samples):
    sources, examples = {}, set()
    for source in PRICE_SEMANTICS:
        hashes = sorted({r["provider_sha256"] for r in samples if r["source"] == source})
        if hashes:
            examples.update((hashes[0], hashes[-1]))
        sources[source] = {"provider_hashes_count": len(hashes),
            "provider_hash_set_sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
            "raw_hashes_retained_in": "verified-input-bundles"}
    # Keep actual provider hashes distinct from their set commitment. The
    # complete raw-hash set remains in the input bundles; copying months of
    # per-minute hashes into every artifact would exceed Drive's 2.2 MB cap.
    return {"supplemental_provider_sha256": sorted(examples), "supplemental_hash_sets": sources}


def _training_frames(frames: dict, *, asset: str) -> tuple[dict, dict]:
    """Admit native archive streams on the live five-minute feature clock.

    Manifest streams use venue|contract|quote; CSV dry runs use venue|asset.
    Native candles close one millisecond before the feature boundary. Only
    that exact offset (or an existing boundary) is accepted, without changing
    the source frames shared by other trainers or collapsing stream identity.
    """
    import pandas as pd

    selected, dispersion = {}, {}
    for key, (candles, features) in frames.items():
        parts = key.split("|")
        if len(parts) not in (2, 3) or not all(parts):
            raise ValueError("invalid XMarket archive stream identity")
        source_asset = parts[1] if len(parts) == 2 else asset_for_contract(parts[1])
        if source_asset != asset:
            continue
        if (not isinstance(candles.index, pd.DatetimeIndex) or candles.index.tz is None
                or not candles.index.equals(features.index)):
            raise ValueError("XMarket candle/feature clocks must match and be timezone-aware")
        closes = candles.index.tz_convert("UTC").ceil("5min")
        offset = closes - candles.index
        if (not ((offset == pd.Timedelta(0)) | (offset == pd.Timedelta(milliseconds=1))).all()
                or not closes.is_unique or not closes.is_monotonic_increasing):
            raise ValueError("invalid XMarket five-minute candle close grid")
        alias = f"{parts[0]}|{asset}"
        if alias in dispersion:
            raise ValueError("ambiguous XMarket venue/asset archive sources")
        k, f = candles.copy(deep=False), features.copy(deep=False)
        k.index = f.index = closes
        selected[key] = k, f
        dispersion[alias] = k, f
    return selected, dispersion


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
    market_data: dict | None = None,
) -> dict:
    """Train one (asset, horizon) artifact and gate it.

    frames: stream key -> (candles DataFrame, archive features
    DataFrame). Returns {"publish_kind": "model"|"report", "artifact":
    doc|None, "report": {...}}. A failed gate publishes a report only.
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
    frames, dispersion_frames = _training_frames(frames, asset=asset)
    market_data = market_data or {}
    if market_data.get("asset", asset) != asset:
        raise ValueError("cross-asset XMarket training inputs")
    samples = market_data.get("samples", [])
    fidelity = max((r["fidelity_minutes"] for r in samples if r["source"] == "polymarket"), default=1)
    live_venues = market_data.get("live_venues", [])
    venues = tuple(v for v in VENUES if v in live_venues and f"{v}|{asset}" in dispersion_frames)
    venues = venues if len(venues) >= 2 else ()
    names = feature_names(venues)
    report_base = {
        "schema": SCHEMA, "family": FAMILY, "asset": asset, "horizon_minutes": horizon,
        "feature_contract": FEATURE_CONTRACT, "input_count": len(names),
        "feature_digest": feature_digest(venues, fidelity),
        "model_training_from_supplements_enabled": True,
        "features": list(names), "dispersion_venues": list(venues),
        "price_semantics": PRICE_SEMANTICS, "native_horizon_minutes": 15,
        "cross_horizon_input": horizon != 15, "pm_fidelity_minutes": fidelity,
        "data_checks": market_data.get("verification", {}),
        "data_errors": market_data.get("errors", []),
        "native_history_acquisition": market_data.get("backfill_progress", {}),
        "venue_frame_coverage": {key: {"rows": len(k),
            "first": str(k.index.min()) if len(k) else None,
            "last": str(k.index.max()) if len(k) else None}
            for key, (k, _) in frames.items()},
        "source_feature_coverage": {}, "row_admission": {},
        "provenance": provenance,
        "overlap_evidence": {"eligible": False, "matching": "exact UTC feature-close timestamps",
                             "minimum_rows": {"train": 300, "validation": 100, "test": 100},
                             "train_before": str(train_end), "test_from": str(test_start),
                             "through": str(trained_through), "purge_minutes": horizon,
                             "by_stream": {}, "splits": {}},
        "effective_window": {"rows": 0, "first": None, "last": None},
        "lead_lag": {"lag_bars": 1, "correlation": None, "rows": 0},
        "gates": GATES, "gates_passed": False,
    }
    def blocked(reason):
        return {"publish_kind": "report", "artifact": None,
                "report": {**report_base, "gate_failures": [reason], "training_status": "blocked"}}
    if not samples:
        return blocked("missing verified prediction-market history")
    if not all(market_data.get("verification", {}).get(source, {}).get("status") == "verified"
               for source in PRICE_SEMANTICS):
        return blocked("prediction-market fidelity/aggregation overlap not verified")
    bars = sampled_bars(samples, asset=asset, pm_fidelity=fidelity)
    bar_clocks = {source: pd.DatetimeIndex(list(rows), tz="UTC") for source, rows in bars.items()}
    report_base["source_feature_coverage"] = {
        source: {"rows": len(rows), "first": str(min(rows)) if rows else None,
                 "last": str(max(rows)) if rows else None}
        for source, rows in bars.items()
    }
    import numpy as np

    parts, coverage_frames = [], {}
    for key, (k, f) in frames.items():
        through = pd.to_datetime(trained_through, utc=True)
        k, f = k.loc[:through], f.loc[:through]
        source_overlap = {
            source: k.index.intersection(clock) for source, clock in bar_clocks.items()
        }
        exact = k.index
        for clock in bar_clocks.values():
            exact = exact.intersection(clock)
        report_base["overlap_evidence"]["by_stream"][key] = {
            "source_rows": {source: len(stamps) for source, stamps in source_overlap.items()},
            "all_sources_rows": len(exact),
            "first": str(exact[0]) if len(exact) else None, "last": str(exact[-1]) if len(exact) else None,
        }
        part = frame_features(k, f, asset=asset, samples=samples, frames=dispersion_frames,
                              venues=venues, pm_fidelity=fidelity)
        part["y"] = build_labels(k, horizon)
        part["stream"] = str(key)
        # Diagnostic only: no future return is ever included in a feature.
        future = k["close"].shift(-1) / k["close"] - 1
        part["future_return"] = future
        finite = part.replace([np.inf, -np.inf], np.nan)
        report_base["row_admission"][key] = {
            "candle_rows": len(k),
            "candle_feature_rows": int(finite[list(BASE_FEATURES)].notna().all(axis=1).sum()),
            "market_feature_rows": int(finite[list(MARKET_FEATURES)].notna().all(axis=1).sum()),
            "dispersion_feature_rows": int(finite[list(DISPERSION_FEATURES)].notna().all(axis=1).sum())
            if venues else None,
            "labelled_rows": int(finite["y"].notna().sum()),
            "complete_rows": int(finite[["y", *names]].notna().all(axis=1).sum()),
        }
        parts.append(part)
        coverage_frames[key] = (k, f)
    if not parts:
        return blocked("no frames to train on")
    data = pd.concat(parts).sort_index()
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=["y", *names])
    report_base["effective_window"] = {"rows": len(data),
        "unique_bars": int(data.index.nunique()),
        "first": str(data.index.min()) if len(data) else None,
        "last": str(data.index.max()) if len(data) else None}
    # pm change at t predicts the later perp return; calculated on one
    # source stream, with exact 5m timestamp alignment across missing rows.
    diag = data[data["stream"] == min(coverage_frames)]
    pm = pd.Series([r["price"] for r in bars["polymarket"].values()],
                   index=pd.DatetimeIndex(list(bars["polymarket"]), tz="UTC"), dtype=float)
    changes = pm - pm.reindex(pm.index - pd.Timedelta(minutes=5)).set_axis(pm.index)
    d = pd.concat([changes.rename("change"), diag["future_return"].rename("return")], axis=1).dropna()
    corr = d["change"].corr(d["return"]) if len(d) > 2 and d["change"].nunique() > 1 and d["return"].nunique() > 1 else float("nan")
    report_base["lead_lag"] = {"lag_bars": 1, "model_horizon_minutes": horizon, "return_horizon_minutes": 5,
        "correlation": float(corr) if math.isfinite(corr) else None, "rows": len(d)}
    train_mask, val_mask, test_mask = chronological_splits(
        data.index, train_end=train_end, test_start=test_start, horizon=horizon
    )
    train, val, test = data[train_mask], data[val_mask], data[test_mask]
    report_base["split_rows"] = {"train": len(train), "validation": len(val), "test": len(test)}
    for name, split in (("train", train), ("validation", val), ("test", test)):
        required = report_base["overlap_evidence"]["minimum_rows"][name]
        report_base["overlap_evidence"]["splits"][name] = {
            "rows": len(split), "unique_bars": int(split.index.nunique()),
            "days": int(split.index.normalize().nunique()),
            "first": str(split.index.min()) if len(split) else None,
            "last": str(split.index.max()) if len(split) else None,
            "shortfall": max(0, required - len(split)),
        }
    report_base["overlap_evidence"]["eligible"] = all(
        s["shortfall"] == 0 for s in report_base["overlap_evidence"]["splits"].values())
    if len(data) < 300:
        return blocked(f"not enough labelled overlap rows: {len(data)}")
    if len(train) < 300 or len(val) < 100 or len(test) < 100:
        return blocked(f"split too small: train={len(train)} val={len(val)} test={len(test)}")
    cols = list(names)
    mean, std = _standardize_fit(train[cols].to_numpy(dtype=float))
    x_train = _standardize_apply(train[cols].to_numpy(dtype=float), mean, std)
    x_val = _standardize_apply(val[cols].to_numpy(dtype=float), mean, std)
    x_test = _standardize_apply(test[cols].to_numpy(dtype=float), mean, std)
    weights = _fit_logistic(x_train, train["y"].to_numpy(dtype=float))
    base = float(train["y"].mean())
    p_val_raw = _raw_probabilities(weights, x_val)
    p_test_raw = _raw_probabilities(weights, x_test)
    # Calibration (D8): Platt fitted on the validation window ONLY, gated
    # on the disjoint test window. Never inherit flow_v3's fitted (a, b).
    fit_rows = [(float(p), int(y)) for p, y in zip(p_val_raw, val["y"].to_numpy(dtype=float))]
    eval_rows = [(float(p), int(y)) for p, y in zip(p_test_raw, test["y"].to_numpy(dtype=float))]
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
    val_metrics = _metrics(p_val, val["y"].to_numpy(dtype=float), base, val.index)
    test_metrics = _metrics(p_test, test["y"].to_numpy(dtype=float), base, test.index)
    holdout = venue_holdout(data, test, weights, mean, std, base, platt if calibration_ok else None, cols=cols)
    coverage = frame_coverage(coverage_frames, months_requested=months_requested)
    coverage["months_in_train"] = sorted(set(train.index.strftime("%Y-%m")))
    coverage["days_in_train"] = len(set(train.index.normalize()))
    gates_passed = bool(
        val_metrics["brier_gain"] > GATES["min_validation_gain"]
        and test_metrics["brier_gain"] > GATES["min_test_gain"]
        and test_metrics["daily_positive_fraction"] >= GATES["min_test_daily_positive"]
        and test_metrics["ece"] <= GATES["max_test_ece"]
        and calibration_ok
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
    evaluation = {
        "validation": val_metrics,
        "test": test_metrics,
        "context_absent_test": None,
        "venue_holdout_28d": holdout,
        "train_rows": len(train),
        "train_before": str(train_end),
        "test_from": str(test_start),
    }
    report = {
        **report_base,
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
        "provenance": {**provenance, **_supplement_provenance(samples)},
        "input_bundle_receipts": market_data.get("receipts", []),
        "code_version": code_version,
        "evaluation_status": "held-out evaluated",
        "serving_capable_without_binance_context": "binance" not in venues,
        "serve_clamp": list(SERVE_CLAMP),
    }
    if not gates_passed:
        return {"publish_kind": "report", "artifact": None, "report": report}
    doc = {
        **report,
        "model_version": MODEL_VERSION,
        "features": cols,
        "feature_sources": {name: ["candles" if name in BASE_FEATURES else "native15-market-history"] for name in names},
        "feature_digest": feature_digest(venues, fidelity),
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
        "training_rows": len(train),
        "created_at": datetime.now(UTC).isoformat(),
        "weights_sha256": hashlib.sha256(
            json.dumps([float(w) for w in weights], separators=(",", ":")).encode()
        ).hexdigest(),
    }
    return {"publish_kind": "model", "artifact": doc, "report": report}


def venue_holdout(data, test, weights, mean, std, base: float, platt: dict | None, *, cols) -> dict:
    """Per-stream Brier gain on the last 28 days of the test slice."""
    import numpy as np
    import pandas as pd

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
        raise ValueError("not a xmarket model document")
    if doc.get("family") != FAMILY or doc.get("model_version") != MODEL_VERSION:
        raise ValueError("xmarket model identity mismatch")
    if doc.get("feature_contract") != FEATURE_CONTRACT:
        raise ValueError("xmarket model feature contract mismatch")
    names = feature_names(doc.get("dispersion_venues", ()))
    if tuple(doc.get("features", ())) != names:
        raise ValueError("xmarket model built for different inputs")
    if doc.get("feature_digest") != feature_digest(doc.get("dispersion_venues", ()), doc.get("pm_fidelity_minutes", 1)):
        raise ValueError("xmarket model feature digest mismatch")
    weights = doc.get("weights")
    if not isinstance(weights, list) or len(weights) != len(names) + 1:
        raise ValueError("xmarket model weights have the wrong width")
    if not all(not isinstance(w, bool) and isinstance(w, (int, float)) and math.isfinite(w) for w in weights):
        raise ValueError("xmarket model weights are not finite")
    calibration = doc.get("calibration") or {}
    if calibration.get("status") != "evaluated":
        raise ValueError("xmarket model calibration did not pass its gate")
    if not all(
        isinstance(calibration.get(k), (int, float)) and math.isfinite(calibration[k])
        for k in ("a", "b")
    ):
        raise ValueError("xmarket model calibration parameters are invalid")
    provenance = doc.get("provenance") or {}
    provider = provenance.get("provider_sha256_by_month")
    if not isinstance(provider, dict) or not provider:
        raise ValueError("xmarket model provenance lacks provider hashes")
    for month, sha in provider.items():
        if not isinstance(sha, str) or not _SHA64.fullmatch(sha):
            raise ValueError(f"xmarket model provider hash for {month} is not a SHA-256")
    normalized = provenance.get("normalized_sha256_by_month")
    if isinstance(normalized, dict):
        # P1-4 lesson: a normalized NDJSON hash must never stand in as a
        # provider hash. Any month whose provider hash equals its normalized
        # hash is refused.
        for month, sha in normalized.items():
            if provider.get(month) == sha:
                raise ValueError(f"xmarket model provider hash for {month} is the normalized hash")
    if not isinstance(provenance.get("months_without_provider_hash"), list):
        raise TypeError("xmarket model provenance lacks months_without_provider_hash")
    overlap = doc.get("overlap_evidence")
    if overlap is not None or provenance.get("recorded_partitions"):
        if (not isinstance(overlap, dict) or overlap.get("eligible") is not True
                or overlap.get("minimum_rows") != {"train": 300, "validation": 100, "test": 100}):
            raise ValueError("XMarket exact overlap/split eligibility not established")
        for name, required in (("train", 300), ("validation", 100), ("test", 100)):
            split = overlap.get("splits", {}).get(name, {})
            count = split.get("rows")
            if (type(count) is not int or count < required or split.get("shortfall") != 0
                    or doc.get("split_rows", {}).get(name) != count):
                raise ValueError("XMarket overlap split counts failed unchanged minimums")
    if doc.get("asset") not in ASSETS or doc.get("horizon_minutes") not in HORIZONS:
        raise ValueError("invalid XMarket artifact scope")
    if (doc.get("input_count") != len(names) or doc.get("price_semantics") != PRICE_SEMANTICS
            or doc.get("native_horizon_minutes") != 15
            or doc.get("cross_horizon_input") is not (doc["horizon_minutes"] != 15)):
        raise ValueError("XMarket input semantics mismatch")
    if doc.get("pm_fidelity_minutes") not in (1, 5, 15):
        raise ValueError("invalid XMarket PM fidelity")
    standardizer = doc.get("standardizer") or {}
    for key in ("mean", "std"):
        values = standardizer.get(key)
        if (not isinstance(values, list) or len(values) != len(names)
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) or key == "std" and v <= 0 for v in values)):
            raise ValueError("invalid XMarket train-only standardizer")
    supplemental = provenance.get("supplemental_provider_sha256")
    if (not isinstance(supplemental, list) or len(supplemental) < 2
            or any(not isinstance(sha, str) or not _SHA64.fullmatch(sha) for sha in supplemental)):
        raise ValueError("XMarket lacks prediction-market provider hashes")
    digest = hashlib.sha256(json.dumps(weights, separators=(",", ":")).encode()).hexdigest()
    if doc.get("weights_sha256") != digest:
        raise ValueError("XMarket weights digest mismatch")
    from parlay_bot.research.xmarket_features import timestamp
    start, end = (timestamp(doc[k]) for k in ("trained_from", "trained_through"))
    if start >= end or doc.get("evaluation_status") != "held-out evaluated":
        raise ValueError("XMarket training window/evaluation invalid")
    if doc.get("gates") != GATES or not all(
        doc.get("data_checks", {}).get(source, {}).get("status") == "verified"
        for source in PRICE_SEMANTICS
    ):
        raise ValueError("XMarket data/offline gates do not match")
    evaluation = doc.get("evaluation") or {}
    val, test = evaluation.get("validation") or {}, evaluation.get("test") or {}
    checks = (val.get("brier_gain"), test.get("brier_gain"),
              test.get("daily_positive_fraction"), test.get("ece"))
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in checks)
            or checks[0] <= GATES["min_validation_gain"]
            or checks[1] <= GATES["min_test_gain"]
            or checks[2] < GATES["min_test_daily_positive"]
            or checks[3] > GATES["max_test_ece"]
            or calibration.get("method") != "platt"):
        raise ValueError("XMarket artifact evaluation failed its gates")
    if doc.get("gates_passed") is not True:
        raise ValueError("xmarket model did not pass its offline gates")
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
        from parlay_bot.research.xmarket_features import timestamp
        trained = timestamp(doc["trained_through"])
        return trained < (now or datetime.now(UTC))
    except (KeyError, ValueError, TypeError):
        return False


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def probability(doc: dict, vector) -> float:
    """Train-standardized, Platt-calibrated probability; missing inputs are rejected."""
    import numpy as np

    validate(doc)
    if len(vector) != len(doc["features"]) or not all(math.isfinite(v) for v in vector):
        raise ValueError("wrong xmarket feature count")
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
    """Continuity identity for the Phase 2 selected() branch: feature
    digest equal, trained_through / graded_count monotone. Weights SHA
    is recomputed from the stored weights, never trusted from a stored
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
            artifact=item, batch_id=f"xmarket-v1-{item.sha256[:24]}", kind=kind
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
        description="Train the xmarket_v1 offline logistic (standalone; the weekly "
        "job runs it through train_weekly_archive instead)."
    )
    parser.add_argument("--inputs", type=Path, required=True, help="Verified xmarket input bundle")
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
        required=True,
        help="Last bar time the artifact was trained through, ISO",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--code-version", default=None)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish kind=model (pass) or kind=report (fail) to Drive",
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
    training = {
        "asset": args.asset,
        "horizon": args.horizon,
        "train_end": train_end,
        "test_start": test_start,
        "trained_through": args.trained_through,
        "provenance": provenance,
        "code_version": args.code_version or _git_sha(),
        "months_requested": None,
    }
    from parlay_bot.research.xmarket_history import load_local_inputs
    result = train_run(frames, **training, market_data=load_local_inputs(args.inputs, asset=args.asset))
    report = dict(result["report"])
    if census is not None:
        report["census"] = census
    out_dir = args.out
    report_path = _write_json(
        out_dir / f"xmarket-v1-{args.asset}-{args.horizon}m-report.json", report
    )
    published = {}
    if result["publish_kind"] == "model":
        doc = validate(result["artifact"])
        artifact_path = _write_json(
            out_dir / f"xmarket-v1-{args.asset}-{args.horizon}m-model.json", doc
        )
        print(f"xmarket-v1 {args.asset} {args.horizon}m: gates PASSED, artifact {artifact_path}")
        if args.publish:
            published = asyncio.run(_publish("model", artifact_path))
    else:
        print(
            f"xmarket-v1 {args.asset} {args.horizon}m: gates FAILED "
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
