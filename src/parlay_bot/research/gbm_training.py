"""Offline trainer for the GBM challenger (gbm_v1).

Autonomous loop Phase 2, spec §3. D4/D7/D8: LightGBM on the 32-feature
archive contract, same frames/labels/splits/gates/provenance as the
archive_v1 logistic, so the logistic-vs-GBM comparison holds the data
fixed and changes only the model class. No serving path, no Postgres
writes, no promotion-table writes anywhere in this module.

Model (D4): LightGBM starting from the pooled ``PARAMS``
(``pooled_training.py`` — 400 trees, lr 0.02, ``num_leaves`` 31,
``max_depth`` 6, ``min_child_samples`` 500, subsample 0.8, colsample 0.8,
``reg_lambda`` 10). Any tuning is validation-only; the parameters used
are recorded in the artifact. The HistGradientBoosting fallback (spec
§3.3) is NOT built: LightGBM's native dependency is present in the
``ml-research`` extra the GHA runner installs (verified 4.7.0), and an
HGB portable export does not exist in this tree (``boosted_portable``
exports LightGBM/XGBoost/CatBoost only). If the import ever fails, the
trainer raises a clear error naming the fallback instead of silently
training a different model class.

Trees are scale-invariant, so unlike the logistic the GBM trains on RAW
feature values with NaN preserved for missing groups (LightGBM learns
the missing direction per split; the portable evaluator reproduces it
from the exported ``missing_type``). Standardising would erase the
missing-data signal. The live vector returns the same raw values with
NaN for missing groups, so train and serve see identical inputs.

Labels, splits, gates, calibration and provenance mirror
``archive_training.train_run`` exactly: chronological train/val/test
with a one-horizon purge; settlement-math labels (strict-UP, flat is
NOT UP, zero-trade targets withdrawn, anti-backfill ordering); offline
gates val gain > 0.005 / test gain > 0.005 / test daily-positive >= 0.55
/ test ECE <= 0.03; Platt fitted on the validation window only, gated
ECE <= 0.10 + top-bucket on the disjoint test window; Track C
context-absent bar (Binance tape group set to NaN, as the live path
does when the bundle is absent) must pass the same 0.005 test-gain bar.
A run that fails any gate publishes ``kind="report"`` only.

The published artifact is the evaluated fit -- never refit on the test
slice (the P1-7 lesson). Serving evaluates the exported trees in pure
Python (the pooled precedent): training refuses to publish unless the
portable evaluator reproduces native LightGBM within 1e-6 on a sampled
test slice. For trees, the "weights SHA" is the hash of the
exported-trees JSON (spec §3.4).
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
from parlay_bot.research.archive_features import (
    FEATURE_CONTRACT,
    FEATURE_DIGEST,
    FEATURE_SOURCES,
    FEATURES,
    GROUPS,
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
from parlay_bot.research.pooled_training import (
    PARAMS,
    export_lightgbm,
    frame_coverage,
    metrics,
)

SCHEMA = "rival-gbm-model-v1"
FAMILY = "gbm_v1"
MODEL_VERSION = "gbm-offline-v1"
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
MAX_TREES = 1000
MAX_NODES = 200_000
_SHA64 = __import__("re").compile(r"[0-9a-f]{64}")


def _platt_apply(a: float, b: float, p):
    import numpy as np

    logit = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    out = 1 / (1 + np.exp(-np.clip(a * logit + b, -30, 30)))
    return np.clip(out, *SERVE_CLAMP)


def _trees_sha256(trees) -> str:
    return hashlib.sha256(
        json.dumps(trees, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_trees(trees, width: int) -> None:
    """Portable-tree shape check (the pooled_model.validate node rules)."""
    if not isinstance(trees, list) or not 1 <= len(trees) <= MAX_TREES:
        raise ValueError("invalid GBM tree count")
    total = 0
    for tree in trees:
        if not isinstance(tree, list) or not tree:
            raise ValueError("invalid GBM tree")
        total += len(tree)
        for position, node in enumerate(tree):
            if len(node) == 1:
                if not math.isfinite(node[0]):
                    raise ValueError("invalid GBM leaf")
                continue
            feature, threshold, default_left, missing, left, right = node
            if (type(feature) is not int or not 0 <= feature < width
                    or not math.isfinite(threshold) or default_left not in (0, 1)
                    or missing not in (0, 1, 2)
                    or type(left) is not int or type(right) is not int
                    or not (position < left < len(tree) and position < right < len(tree))):
                raise ValueError("invalid GBM split")
    if total > MAX_NODES:
        raise ValueError("oversized GBM model")


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
    params: dict | None = None,
) -> dict:
    """Train one (asset, horizon) GBM artifact and gate it.

    frames: stream key -> (candles DataFrame, features DataFrame).
    Returns {"publish_kind": "model"|"report", "artifact": doc|None,
    "report": {...}}. A failed gate publishes a report only. ``params``
    defaults to the pooled PARAMS; any override is recorded in the
    artifact (validation-only tuning).
    """
    import pandas as pd

    from parlay_bot.perpetuals.flow_challenger_v3 import (
        calibration_gate,
        fit_platt,
    )

    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise ValueError(
            "LightGBM is not installed; the spec D4 HistGradientBoosting "
            "fallback is not built (no portable HGB export exists in this "
            "tree). Install the ml-research extra."
        ) from exc

    if asset not in ASSETS:
        raise ValueError(f"unsupported asset: {asset!r}")
    if horizon not in HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon!r}")
    params = dict(params if params is not None else PARAMS)
    anchor = tuple(
        GROUPS["candle"] + GROUPS["multi_horizon"] + GROUPS["calendar"] + GROUPS["cross_asset"]
    )
    if asset == "BTC":
        # A BTC stream is not its own BTC lead: btc_ret_1/12 and
        # rel_ret_12 are NaN by pooled design; only "coin" anchors.
        anchor = tuple(
            i for i in anchor if FEATURES[i] not in ("btc_ret_1", "btc_ret_12", "rel_ret_12")
        )
    parts, coverage_frames = [], {}
    for key, (k, f) in frames.items():
        part = f.copy()
        part["y"] = build_labels(k, horizon)
        part["stream"] = str(key)
        parts.append(part)
        coverage_frames[key] = (k, f)
    if not parts:
        raise TrainingDataUnavailable("no frames to train on")
    data = pd.concat(parts).sort_index()
    # Anchor groups must be present; context groups may be missing (NaN
    # is a real LightGBM input here, not a value to fill).
    data = data.dropna(subset=["y", *[FEATURES[i] for i in anchor]])
    if len(data) < 300:
        raise TrainingDataUnavailable(f"not enough labelled rows: {len(data)}")
    train_mask, val_mask, test_mask = chronological_splits(
        data.index, train_end=train_end, test_start=test_start, horizon=horizon
    )
    train, val, test = data[train_mask], data[val_mask], data[test_mask]
    if len(train) < 300 or len(val) < 100 or len(test) < 100:
        raise TrainingDataUnavailable(
            f"split too small: train={len(train)} val={len(val)} test={len(test)}"
        )
    cols = list(FEATURES)
    model = lgb.LGBMClassifier(**params).fit(train[cols], train["y"])
    base = float(train["y"].mean())
    p_val_raw = model.predict_proba(val[cols])[:, 1]
    p_test_raw = model.predict_proba(test[cols])[:, 1]
    # Calibration (D8): Platt fitted on the validation window ONLY, gated
    # on the disjoint test window. Never inherit another family's (a, b).
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
    val_metrics = metrics(p_val, val["y"].to_numpy(dtype=float), base, val.index)
    test_metrics = metrics(p_test, test["y"].to_numpy(dtype=float), base, test.index)
    # Track C: context-absent evaluation -- the Binance tape group set to
    # NaN, exactly as the live vector does when the bundle is absent.
    # Must pass the same 0.005 bar, or the artifact is not serving-capable
    # on the production host.
    test_absent = test.copy()
    test_absent[[FEATURES[i] for i in GROUPS["binance_tape"]]] = float("nan")
    p_absent_raw = model.predict_proba(test_absent[cols])[:, 1]
    p_absent = (
        _platt_apply(float(platt["a"]), float(platt["b"]), p_absent_raw)
        if calibration_ok
        else p_absent_raw
    )
    absent_metrics = metrics(p_absent, test["y"].to_numpy(dtype=float), base, test.index)
    context_absent_ok = absent_metrics["brier_gain"] > MIN_CONTEXT_ABSENT_GAIN
    # 28-day venue holdout, per stream, on the test slice.
    holdout = venue_holdout(data, test, model, cols, base, platt if calibration_ok else None)
    coverage = frame_coverage(coverage_frames, months_requested=months_requested)
    coverage["months_in_train"] = sorted(set(train.index.strftime("%Y-%m")))
    coverage["days_in_train"] = len(set(train.index.normalize()))
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
        "train_rows": len(train),
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
        "provenance": provenance,
        "code_version": code_version,
        "evaluation_status": "held-out evaluated",
        "serving_capable_without_binance_context": context_absent_ok,
        "serve_clamp": list(SERVE_CLAMP),
    }
    if not gates_passed:
        return {"publish_kind": "report", "artifact": None, "report": report}
    trees = export_lightgbm(model)
    # Portable parity (pooled precedent): the bot host has no LightGBM, so
    # the published fit is refused unless the pure-Python evaluator
    # reproduces native LightGBM within 1e-6 on a sampled test slice.
    from parlay_bot.perpetuals.pooled_model import probability as portable_probability

    sample = test[cols].sample(min(2000, len(test)), random_state=1)
    skeleton = {"features": cols, "trees": trees}
    native = model.predict_proba(sample)[:, 1]
    import numpy as np

    portable = np.array(
        [portable_probability(skeleton, tuple(float(x) for x in row))
         for row in sample.itertuples(index=False)]
    )
    error = float(np.max(np.abs(native - portable)))
    if not math.isfinite(error) or error > 1e-6:
        raise ValueError(f"portable evaluator disagrees with LightGBM by {error}")
    doc = {
        **report,
        "model_version": MODEL_VERSION,
        "features": cols,
        "feature_sources": {name: list(FEATURE_SOURCES[name]) for name in FEATURES},
        "feature_digest": FEATURE_DIGEST,
        "trees": trees,
        "params": params,
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
        "weights_sha256": _trees_sha256(trees),
        "portable_max_error": error,
    }
    return {"publish_kind": "model", "artifact": doc, "report": report}


def venue_holdout(data, test, model, cols, base: float, platt: dict | None) -> dict:
    """Per-stream Brier gain on the last 28 days of the test slice."""
    import numpy as np
    import pandas as pd

    out = {}
    if test.empty:
        return out
    start = test.index.max().normalize() - pd.Timedelta(days=HOLDOUT_DAYS)
    for stream, part in test[test.index >= start].groupby("stream"):
        p = model.predict_proba(part[cols])[:, 1]
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
        raise ValueError("not a GBM model document")
    if doc.get("family") != FAMILY or doc.get("model_version") != MODEL_VERSION:
        raise ValueError("GBM model identity mismatch")
    if doc.get("feature_contract") != FEATURE_CONTRACT:
        raise ValueError("GBM model feature contract mismatch")
    if tuple(doc.get("features", ())) != FEATURES:
        raise ValueError("GBM model built for different inputs")
    if doc.get("feature_digest") != FEATURE_DIGEST:
        raise ValueError("GBM model feature digest mismatch")
    _validate_trees(doc.get("trees"), len(FEATURES))
    if doc.get("weights_sha256") != _trees_sha256(doc["trees"]):
        raise ValueError("GBM model weights SHA does not match its trees")
    if not isinstance(doc.get("params"), dict) or not doc["params"]:
        raise ValueError("GBM model params are missing")
    calibration = doc.get("calibration") or {}
    if calibration.get("status") != "evaluated":
        raise ValueError("GBM model calibration did not pass its gate")
    if not all(
        isinstance(calibration.get(k), (int, float)) and math.isfinite(calibration[k])
        for k in ("a", "b")
    ):
        raise ValueError("GBM model calibration parameters are invalid")
    provenance = doc.get("provenance") or {}
    provider = provenance.get("provider_sha256_by_month")
    if not isinstance(provider, dict) or not provider:
        raise ValueError("GBM model provenance lacks provider hashes")
    for month, sha in provider.items():
        if not isinstance(sha, str) or not _SHA64.fullmatch(sha):
            raise ValueError(f"GBM model provider hash for {month} is not a SHA-256")
    normalized = provenance.get("normalized_sha256_by_month")
    if isinstance(normalized, dict):
        # P1-4 lesson: a normalized NDJSON hash must never stand in as a
        # provider hash. Any month whose provider hash equals its normalized
        # hash is refused.
        for month, sha in normalized.items():
            if provider.get(month) == sha:
                raise ValueError(f"GBM model provider hash for {month} is the normalized hash")
    if not isinstance(provenance.get("months_without_provider_hash"), list):
        raise TypeError("GBM model provenance lacks months_without_provider_hash")
    if doc.get("gates_passed") is not True:
        raise ValueError("GBM model did not pass its offline gates")
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

    The portable evaluator walks the exported trees in pure Python (the
    bot host has no LightGBM); Platt calibration is applied on top, then
    the serve clamp [0.02, 0.98]. Non-finite values other than NaN are
    treated as missing, never as a number.
    """
    import math as _math

    from parlay_bot.perpetuals.pooled_model import probability as portable_probability

    validate(doc)
    if len(vector) != len(FEATURES):
        raise ValueError("wrong GBM feature count")
    clean = tuple(
        float(v) if _math.isfinite(float(v)) else float("nan") for v in vector
    )
    raw = portable_probability(doc, clean)
    calibration = doc["calibration"]
    logit = _math.log(max(1e-6, min(1 - 1e-6, raw)) / (1 - max(1e-6, min(1 - 1e-6, raw))))
    calibrated = 1 / (1 + _math.exp(-max(-30.0, min(30.0, float(calibration["a"]) * logit + float(calibration["b"])))))
    return float(min(SERVE_CLAMP[1], max(SERVE_CLAMP[0], calibrated)))


def artifact_identity(doc: dict) -> dict:
    """Pinning identity for the Phase 2 selected() branch (spec §3.4, D14):
    feature digest equal, trained-through / graded-count monotone.
    Weights SHA is the hash of the exported-trees JSON, recomputed from
    the stored trees, never trusted from a stored column."""
    validate(doc)
    return {
        "feature_digest": doc["feature_digest"],
        "weights_sha256": _trees_sha256(doc["trees"]),
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
            artifact=item, batch_id=f"gbm-v1-{item.sha256[:24]}", kind=kind
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
    parser = argparse.ArgumentParser(description="Train the gbm_v1 offline LightGBM (Phase 2).")
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
        frames, provenance = load_frames_manifests(args.manifests_dir)
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
        out_dir / f"gbm-v1-{args.asset}-{args.horizon}m-report.json", report
    )
    published = {}
    if result["publish_kind"] == "model":
        doc = validate(result["artifact"])
        artifact_path = _write_json(
            out_dir / f"gbm-v1-{args.asset}-{args.horizon}m-model.json", doc
        )
        print(f"gbm-v1 {args.asset} {args.horizon}m: gates PASSED, artifact {artifact_path}")
        if args.publish:
            published = asyncio.run(_publish("model", artifact_path))
    else:
        print(
            f"gbm-v1 {args.asset} {args.horizon}m: gates FAILED "
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
