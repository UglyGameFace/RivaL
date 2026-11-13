"""Offline trainer for the archive-serving model (archive_v1).

Spec §2.3-§2.7, decisions D1/D2/D4-D8. Phase 1 only: training job +
artifact/provenance + offline gates. No serving path, no Postgres writes,
no promotion-table writes anywhere in this module.

Model (D1, option A): batch-trained logistic per (asset, horizon) on the
32 archive features (archive_features.FEATURES). Batch gradient descent
on standardised inputs (standardiser fitted on the train slice only);
the prequential discipline is preserved by the chronological split: no
label known after an issuance influences that issuance's score, and the
published artifact is the evaluated fit -- never refit on the test slice
(the P1-7 lesson; pooled_model.py:130-145 precedent).

Labels are built by the settlement math (§1.7.3): strict-UP, flat is
NOT UP, zero-trade targets withdrawn, anti-backfill ordering on archive
timestamps (the outcome bar's first observation must be after the
issuance bar's close).

Offline gates (D7, pooled precedent): validation Brier gain > 0.005,
test Brier gain > 0.005, test daily-positive fraction >= 0.55,
test ECE <= 0.03. Calibration (D8): Platt fitted on the validation
window only (flow_challenger_v3.fit_platt), gated ECE <= 0.10 +
top-bucket on the disjoint test window; a failed calibration gate means
the artifact does not ship -- never "disclosed raw". Track C (§2.10.3):
a context-absent evaluation (Binance tape group flagged off) must pass
the same 0.005 test-gain bar, or the artifact does not ship as
serving-capable on the production host (Binance fapi returns 451 there).
A run that fails any gate publishes kind="report" only -- no model
artifact. Serve-time clamp [0.02, 0.98] is recorded here; Phase 2
implements the serving path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from parlay_bot.perpetuals.pooled_features import ASSETS
from parlay_bot.research.archive_features import (
    FEATURE_CONTRACT,
    FEATURE_DIGEST,
    FEATURE_SOURCES,
    FEATURES,
    GROUPS,
    MODEL_VERSION,
    frame_features,
)

SCHEMA = "rival-archive-model-v1"
FAMILY = "archive_v1"
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
# Batch gradient descent, mirroring the incumbent SGD constants
# (learning.py: lr 0.025 per sample, L2 1e-4 on non-bias weights).
LEARNING_RATE = 0.5
L2 = 1e-4
EPOCHS = 300
_SHA64 = __import__("re").compile(r"[0-9a-f]{64}")


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def build_labels(k, horizon: int):
    """Strict-UP labels for a candle frame indexed by bar CLOSE time.

    k needs close and trades columns, and optionally first_observed_at.
    Flat target is NOT UP (label 0). A flat target bar with zero trades
    is withdrawn (NaN), not counted as NOT UP. Anti-backfill: when
    first_observed_at is present, the outcome bar must first be observed
    after the issuance bar's close, else the label is NaN.
    """
    import numpy as np
    import pandas as pd

    step = horizon // 5
    c = k["close"]
    future = c.shift(-step)
    ok = (k.index.to_series().shift(-step) - k.index.to_series()) == pd.Timedelta(minutes=horizon)
    y = (future > c).astype("float32")
    y[~ok.to_numpy() | future.isna().to_numpy()] = np.nan
    if "trades" in k.columns:
        flat = (k["trades"].shift(-step) == 0) & (future == c)
        y[flat.to_numpy()] = np.nan
    if "first_observed_at" in k.columns:
        observed = pd.to_datetime(k["first_observed_at"], utc=True).shift(-step)
        valid = observed > k.index.to_series()
        y[~valid.to_numpy()] = np.nan
    return y


def chronological_splits(index, *, train_end, test_start, horizon: int):
    """Chronological train/val/test masks with a one-horizon purge."""
    import pandas as pd

    purge = pd.Timedelta(minutes=horizon)
    train = index < train_end - purge
    val = (index >= train_end) & (index < test_start - purge)
    test = index >= test_start
    return train, val, test


def _standardize_fit(x):
    """Train-only standardiser: mean/std per feature, NaN-aware."""
    import warnings

    import numpy as np

    # An all-NaN column (a group with no data in train) has no mean/std;
    # it standardises to 0 via the fallbacks below.
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


class TrainingDataUnavailable(ValueError):
    """Verified input cannot supply the required chronological splits."""


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
    groups_off: tuple[str, ...] = (),
) -> dict:
    """Train one (asset, horizon) artifact and gate it.

    frames: stream key -> (candles DataFrame, features DataFrame).
    Returns {"publish_kind": "model"|"report", "artifact": doc|None,
    "report": {...}}. A failed gate publishes a report only.
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
    masked_features = _masked_features(groups_off)
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
        if masked_features:
            part[list(masked_features)] = float("nan")
        part["y"] = build_labels(k, horizon)
        part["stream"] = str(key)
        parts.append(part)
        coverage_frames[key] = (k, f)
    if not parts:
        raise TrainingDataUnavailable("no frames to train on")
    data = pd.concat(parts).sort_index()
    # Anchor groups must be present; context groups may be missing.
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
    # Track C (§2.10.3): context-absent evaluation -- the Binance tape
    # group flagged off (standardised inputs forced to the group mean).
    # Must pass the same 0.005 bar, or the artifact is not serving-capable
    # on the production host.
    x_test_absent = x_test.copy()
    x_test_absent[:, list(GROUPS["binance_tape"])] = 0.0
    p_absent_raw = _raw_probabilities(weights, x_test_absent)
    p_absent = (
        _platt_apply(float(platt["a"]), float(platt["b"]), p_absent_raw)
        if calibration_ok
        else p_absent_raw
    )
    absent_metrics = _metrics(p_absent, test["y"].to_numpy(dtype=float), base, test.index)
    context_absent_ok = absent_metrics["brier_gain"] > MIN_CONTEXT_ABSENT_GAIN
    # 28-day venue holdout, per stream, on the test slice.
    holdout = venue_holdout(data, test, weights, mean, std, base, platt if calibration_ok else None)
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
    if groups_off:
        report["feature_groups_off"] = list(groups_off)
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
        "training_rows": len(train),
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


def _masked_features(groups) -> tuple[str, ...]:
    if not isinstance(groups, (list, tuple)) or any(
        group not in ("premium", "binance_tape") for group in groups
    ):
        raise ValueError("unsupported archive feature ablation")
    return tuple(FEATURES[i] for group in groups for i in GROUPS[group])


def validate(doc: dict) -> dict:
    """Fail closed on any artifact that must not enter the import path."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not an archive model document")
    if doc.get("family") != FAMILY or doc.get("model_version") != MODEL_VERSION:
        raise ValueError("archive model identity mismatch")
    if doc.get("feature_contract") != FEATURE_CONTRACT:
        raise ValueError("archive model feature contract mismatch")
    if tuple(doc.get("features", ())) != FEATURES:
        raise ValueError("archive model built for different inputs")
    if doc.get("feature_digest") != FEATURE_DIGEST:
        raise ValueError("archive model feature digest mismatch")
    _masked_features(doc.get("feature_groups_off", ()))
    weights = doc.get("weights")
    if not isinstance(weights, list) or len(weights) != len(FEATURES) + 1:
        raise ValueError("archive model weights have the wrong width")
    if not all(isinstance(w, (int, float)) and math.isfinite(w) for w in weights):
        raise ValueError("archive model weights are not finite")
    calibration = doc.get("calibration") or {}
    if calibration.get("status") != "evaluated":
        raise ValueError("archive model calibration did not pass its gate")
    if not all(
        isinstance(calibration.get(k), (int, float)) and math.isfinite(calibration[k])
        for k in ("a", "b")
    ):
        raise ValueError("archive model calibration parameters are invalid")
    provenance = doc.get("provenance") or {}
    provider = provenance.get("provider_sha256_by_month")
    if not isinstance(provider, dict) or not provider:
        raise ValueError("archive model provenance lacks provider hashes")
    for month, sha in provider.items():
        if not isinstance(sha, str) or not _SHA64.fullmatch(sha):
            raise ValueError(f"archive model provider hash for {month} is not a SHA-256")
    normalized = provenance.get("normalized_sha256_by_month")
    if isinstance(normalized, dict):
        # P1-4 lesson: a normalized NDJSON hash must never stand in as a
        # provider hash. Any month whose provider hash equals its normalized
        # hash is refused.
        for month, sha in normalized.items():
            if provider.get(month) == sha:
                raise ValueError(f"archive model provider hash for {month} is the normalized hash")
    if not isinstance(provenance.get("months_without_provider_hash"), list):
        raise TypeError("archive model provenance lacks months_without_provider_hash")
    if doc.get("gates_passed") is not True:
        raise ValueError("archive model did not pass its offline gates")
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
        raise ValueError("wrong archive feature count")
    mean = np.array(doc["standardizer"]["mean"], dtype=float)
    std = np.array(doc["standardizer"]["std"], dtype=float)
    values = np.array(vector, dtype=float)
    for name in _masked_features(doc.get("feature_groups_off", ())):
        values[FEATURES.index(name)] = float("nan")
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
    """Continuity identity for the Phase 2 selected() branch (spec §2.2):
    feature digest equal, trained_through / graded_count monotone.
    Weights SHA is recomputed from the stored weights, never trusted
    from a stored column."""
    validate(doc)
    return {
        "feature_digest": doc["feature_digest"],
        "weights_sha256": hashlib.sha256(
            json.dumps(doc["weights"], separators=(",", ":")).encode()
        ).hexdigest(),
        "trained_through": doc["trained_through"],
        "graded_count": int(doc.get("graded_count", 0)),
    }


def census_summary(census_path: Path) -> dict:
    """Measure what the Drive census actually lists before training.

    Reads data/native-reports/drive-source-census.json (the output of
    drive-source-census.yml). Names are NOT byte-SHA readbacks; a month
    listed here still has to pass the restore verification per month.
    Any month that fails verification is excluded and listed by the
    restore step, never silently skipped.
    """
    data = json.loads(Path(census_path).read_text(encoding="utf-8"))
    scopes = []
    for row in data.get("month_scopes", []):
        scopes.append(
            {
                "venue": row.get("venue"),
                "asset": row.get("asset"),
                "family": row.get("family"),
                "listed_manifest_months": row.get("listed_manifest_months"),
                "not_listed_months": row.get("not_listed_months"),
                "conflicting_months": row.get("conflicting_months"),
                "duplicate_name_months": row.get("duplicate_name_months"),
            }
        )
    return {
        "census_file": str(census_path),
        "duplicate_manifest_names": data.get("duplicate_manifest_names"),
        "scopes": scopes,
        "note": (
            "Names only: no month is byte-verified by this census; "
            "restore re-proves bytes per month."
        ),
    }


def load_frames_csv(frames_dir: Path) -> dict:
    """Load per-stream CSV frames for a dry run.

    Files: <venue>__<asset>.csv with columns time (bar close, ISO),
    open, high, low, close, volume, trades and optional taker_buy_volume,
    count, prem, fund, first_observed_at. Returns stream key ->
    (candles DataFrame, features DataFrame).
    """
    import pandas as pd

    frames = {}
    for path in sorted(Path(frames_dir).glob("*.csv")):
        venue, asset = path.stem.split("__", 1)
        raw = pd.read_csv(path)
        raw["time"] = pd.to_datetime(raw["time"], utc=True)
        raw = raw.set_index("time").sort_index()
        if "first_observed_at" in raw.columns:
            raw["first_observed_at"] = pd.to_datetime(raw["first_observed_at"], utc=True)
        btc = None
        if asset != "BTC":
            sibling = Path(frames_dir) / f"{venue}__BTC.csv"
            if sibling.exists():
                brow = pd.read_csv(sibling, usecols=["time", "close"])
                brow["time"] = pd.to_datetime(brow["time"], utc=True)
                btc = brow.set_index("time").sort_index()
        frames[f"{venue}|{asset}"] = (raw, frame_features(raw, btc, asset))
    return frames


def load_frames_manifests(root: Path, *, target_asset: str | None = None) -> tuple[dict, dict]:
    """Load verified months restored under ``root`` via the existing
    restore paths (bitget_source_bridge / source_native), grouped per
    stream. Returns (frames, provenance). ``learning.assemble`` re-proves
    each manifest's bytes and grid; any month failing verification raises
    instead of being silently skipped."""
    import pandas as pd

    from parlay_bot.research.learning import assemble
    from parlay_bot.research.recorded_candles import latest_recorded_snapshots

    manifests = sorted(Path(root).rglob("*.manifest.json"))
    if not manifests:
        raise TrainingDataUnavailable(f"no restored manifests under {root}")
    manifests = latest_recorded_snapshots(manifests)
    groups: dict[tuple, list[Path]] = {}
    for path in manifests:
        info = json.loads(path.read_text(encoding="utf-8"))
        key = (info.get("venue"), info.get("contract"), info.get("quote"))
        groups.setdefault(key, []).append(path)
    tables: dict[str, object] = {}
    provenance: dict = {
        "provider_sha256_by_month": {},
        "months_without_provider_hash": [],
        "manifest_sha256_by_month": {},
        "normalized_sha256_by_month": {},
    }
    for key, paths in groups.items():
        identity, rows, prov = assemble(paths)
        venue, contract, quote = (identity["venue"], identity["contract"], identity["quote"])
        from parlay_bot.perpetuals.markets import asset_for_contract

        asset = asset_for_contract(contract)
        if target_asset is not None and asset not in (target_asset, "BTC"):
            continue
        table = pd.DataFrame(rows)
        table["time"] = pd.to_datetime(table["closed_at"], utc=True)
        table = table.rename(columns={"volume_base": "volume"})
        table = table.set_index("time").sort_index()
        tables[f"{venue}|{contract}|{quote}"] = (table, asset)
        if target_asset is not None and asset != target_asset:
            reference = provenance.setdefault("reference_provider_sha256_by_month", {})
            reference.update({f"{venue}:{month}": sha
                              for month, sha in (prov.get("provider_sha256_by_month") or {}).items()})
            for month, evidence in prov.get("recorded_partitions", {}).items():
                provenance.setdefault("reference_recorded_partitions", {})[
                    f"{venue}:{month}"] = evidence
            continue
        for month, sha in (prov.get("provider_sha256_by_month") or {}).items():
            provenance["provider_sha256_by_month"][f"{venue}:{month}"] = sha
        provenance["months_without_provider_hash"].extend(
            f"{venue}:{m}" for m in (prov.get("months_without_provider_hash") or [])
        )
        for month in prov.get("months", []):
            provenance["manifest_sha256_by_month"][f"{venue}:{month}"] = prov.get(
                "manifest_sha256_by_month", {}
            ).get(month)
            provenance["normalized_sha256_by_month"][f"{venue}:{month}"] = prov.get(
                "normalized_sha256_by_month", {}
            ).get(month)
        for month, evidence in prov.get("recorded_partitions", {}).items():
            provenance.setdefault("recorded_partitions", {})[f"{venue}:{month}"] = evidence
    frames = {}
    for stream_key, (table, asset) in tables.items():
        if target_asset is not None and asset != target_asset:
            continue
        venue = stream_key.split("|", 1)[0]
        btc = None
        if asset != "BTC":
            for other_key, (other_table, other_asset) in tables.items():
                if other_key.startswith(venue + "|") and other_asset == "BTC":
                    btc = other_table[["close"]]
                    break
        frames[stream_key] = (table, frame_features(table, btc, asset))
    return frames, provenance


async def restore_verified_months(*, asset: str, months: list[str], root: Path,
                                  records_since: date | None = None,
                                  records_through: date | None = None,
                                  now: datetime | None = None) -> dict:
    """Restore verified months for one asset via the existing bridges.

    Bitget market months via restore_verified_bitget_market_months;
    OKX/Gate via restore_verified_source_months. Each bridge re-downloads
    raw responses + manifest, SHA-checks both, re-parses, and re-proves
    the grid per month. A venue that fails is listed under "excluded"
    with its reason, never silently skipped. Binance 5m is NOT restored
    here: it loads from official Vision ZIPs via load_binance_frame
    (the pooled training path), whose per-month ZIP SHA is verified
    against the provider .CHECKSUM at fetch time.
    """
    from parlay_bot.config import Settings
    from parlay_bot.research.bitget_source_bridge import (
        restore_verified_bitget_market_months,
    )
    from parlay_bot.research.publish import _drive
    from parlay_bot.research.source_native import restore_verified_source_months

    drive = _drive(Settings())
    clock = now or datetime.now(UTC)
    report: dict = {
        "asset": asset,
        "months_requested": list(months),
        "restored": {},
        "excluded": {},
    }
    try:
        assets = (asset,) if asset == "BTC" else (asset, "BTC")
        for coin in assets:
            suffix = "" if coin == asset else ":BTC-reference"
            try:
                paths = await restore_verified_bitget_market_months(
                    drive, months=list(months), root=root, asset=coin,
                )
                report["restored"]["bitget" + suffix] = [str(p) for p in paths]
            except Exception as exc:  # noqa: BLE001 - record, do not skip silently
                report["excluded"]["bitget" + suffix] = {
                    "reason": type(exc).__name__, "detail": str(exc)[:300],
                }
            for venue, contract in (("okx", f"{coin}-USDT-SWAP"), ("gate", f"{coin}_USDT")):
                try:
                    paths = await restore_verified_source_months(
                        drive, venue=venue, months=list(months), root=root, contract=contract,
                    )
                    report["restored"][venue + suffix] = [str(p) for p in paths]
                except Exception as exc:  # noqa: BLE001 - record, do not skip silently
                    report["excluded"][venue + suffix] = {
                        "reason": type(exc).__name__, "detail": str(exc)[:300],
                    }
        from parlay_bot.research.recorded_candles import restore_recorded_partitions

        try:
            recorded = await restore_recorded_partitions(
                drive, asset=asset, root=root,
                first_day=records_since or (clock - timedelta(days=90)).date(),
                last_day=records_through or (clock - timedelta(days=1)).date(), now=clock,
            )
            report["recorded_candles"] = recorded
            report["restored"]["recorded-partial-partitions"] = recorded["restored"]
        except Exception as exc:  # noqa: BLE001 - peer monthly sources remain available
            report["excluded"]["recorded-partial-partitions"] = {
                "reason": type(exc).__name__, "detail": str(exc)[:300],
            }
    finally:
        await drive.aclose()
    _write_json(Path(root) / "restore-report.json", report)
    return report


async def load_binance_frame(asset: str, cache: Path, last_month: str, *,
                             through_day=None, first_month=None):
    """Binance 5m frame via the existing pooled-training loader.

    tools/feature_signal_experiment.load_asset fetches official Vision
    monthly ZIPs (klines, premiumIndexKlines, fundingRate), verifying
    each ZIP's SHA-256 against the provider .CHECKSUM at fetch time.
    Returns (candles, features, coverage). The per-month provider hash
    is verified but not retained by that loader, so Binance months are
    recorded in months_without_provider_hash by the caller.
    """
    import sys

    import httpx

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
    import feature_signal_experiment as source

    # The workflow's actions/cache restore leaves no directory on a cache
    # miss, and the loader writes each verified ZIP straight into it.
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(16)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        k = await source.load_asset(
            client, asset, last_month, cache, sem,
            through_day=through_day, first_month=first_month,
        )
    if "count" in k.columns and "trades" not in k.columns:
        k["trades"] = k["count"]
    btc = None
    if asset != "BTC":
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            btc_k = await source.load_asset(
                client, "BTC", last_month, cache, sem,
                through_day=through_day, first_month=first_month,
            )
        btc = btc_k[["close"]]
    coverage = dict(k.attrs.get("coverage", {}))
    return k, frame_features(k, btc, asset), coverage


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
            artifact=item, batch_id=f"archive-v1-{item.sha256[:24]}", kind=kind
        )
        return {
            "kind": kind,
            "file_id": restored.file_id,
            "sha256": item.sha256,
            "live_predictions_modified": False,
        }
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None, *, weekly: bool = False) -> int:
    parser = argparse.ArgumentParser(description="Train the archive_v1 offline logistic (Phase 1).")
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
    shared_run = weekly and args.manifests_dir is not None and args.census is not None
    train_end = datetime.fromisoformat(args.train_end)
    test_start = datetime.fromisoformat(args.test_start)
    if train_end.tzinfo is None or test_start.tzinfo is None:
        raise ValueError("train-end and test-start must be timezone-aware ISO")
    census = census_summary(args.census) if args.census else None
    if args.manifests_dir is not None:
        try:
            frames, provenance = load_frames_manifests(args.manifests_dir, target_asset=args.asset)
        except TrainingDataUnavailable:
            if not shared_run:
                raise
            frames, provenance = {}, {}
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
    # Without any provider hashes the existing admission validator cannot
    # accept a fit. The weekly job records that blocker without fetching ZIPs.
    if args.binance_cache is not None and (
        not shared_run or provenance.get("provider_sha256_by_month")
    ):
        from datetime import date as _date

        if not args.binance_last_month:
            raise ValueError("--binance-last-month is required with --binance-cache")
        through = (
            _date.fromisoformat(args.binance_through_day) if args.binance_through_day else None
        )
        btc_k, btc_f, btc_cov = asyncio.run(
            load_binance_frame(
                args.asset, args.binance_cache, args.binance_last_month, through_day=through,
                first_month=("2019-09" if args.asset == "XRP" and args.horizon == 5 else "2021-01")
                if shared_run else None,
            )
        )
        frames[f"binance|{args.asset}USDT|USDT"] = (btc_k, btc_f)
        # Binance ZIP SHAs are verified against the provider .CHECKSUM at
        # fetch time but not retained per month by that loader.
        for month in (btc_cov.get("klines") or {}).get("months_loaded", []):
            provenance.setdefault("months_without_provider_hash", []).append(f"binance:{month}")
        provenance["binance_zip_sha256_verified_at_fetch"] = True
        provenance["binance_coverage"] = btc_cov
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
    if shared_run:
        from parlay_bot.research.weekly_archive import train_weekly_archive

        return asyncio.run(train_weekly_archive(
            frames, training=training, census=census, out=args.out, publish=args.publish,
        ))
    result = train_run(frames, **training)
    report = dict(result["report"])
    if census is not None:
        report["census"] = census
    out_dir = args.out
    report_path = _write_json(
        out_dir / f"archive-v1-{args.asset}-{args.horizon}m-report.json", report
    )
    published = {}
    if result["publish_kind"] == "model":
        doc = validate(result["artifact"])
        artifact_path = _write_json(
            out_dir / f"archive-v1-{args.asset}-{args.horizon}m-model.json", doc
        )
        print(f"archive-v1 {args.asset} {args.horizon}m: gates PASSED, artifact {artifact_path}")
        if args.publish:
            published = asyncio.run(_publish("model", artifact_path))
    else:
        print(
            f"archive-v1 {args.asset} {args.horizon}m: gates FAILED "
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
