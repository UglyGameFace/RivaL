"""Offline batch training for the volcal_v1 challenger family.

One Platt-calibrated logistic per (asset, horizon): BTC/ETH/SOL/XRP x
5/15/60 = 12 artifacts. 5-minute gets its own training and evaluation
— never borrowed 15-minute parameters (the hard rule from the
perp-engine review). Fitting math, gates, splits, and the report/model
document shape are shared deliberately with archive_v1
(archive_training): same batch gradient descent, same chronological
train/val/test with a one-horizon purge, same 0.005 Brier-gain bars,
same Platt-on-validation / gate-on-test calibration discipline. The
only differences are the feature contract (volcal_features, 22
features) and the absence of context groups: volcal reads candles
only, so there is no context-absent evaluation to run — serving never
depends on Binance tape, premium, or funding, and
``serving_capable_without_binance_context`` is True by construction.

Training data: the same per-stream frames the archive job loads
(candles + the precomputed archive feature frame). The base 10 features
are taken verbatim from the archive frame's first 10 columns, so the
incumbent-10 definitions cannot drift between the two families; the
new 12 come from volcal_features.frame_new_features on the candles.
Streams are pooled across venues, exactly like archive_v1.

A failed gate publishes a report only (publish_kind "report"); the
artifact is published only when every gate passes. Artifacts enter
the bot only through the Drive import path (bot.py), never directly.
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

from parlay_bot.perpetuals.markets import ASSETS
from parlay_bot.research.archive_training import (
    TrainingDataUnavailable,
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
from parlay_bot.research.volcal_features import (
    BASE_FEATURES,
    FEATURE_CONTRACT,
    FEATURE_DIGEST,
    FEATURE_SOURCES,
    FEATURES,
    GROUPS,
    MODEL_VERSION,
    frame_new_features,
)

SCHEMA = "rival-volcal-model-v1"
FAMILY = "volcal_v1"
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


def _volcal_frame(candles, archive_frame):
    """Join the archive frame's base 10 with the 12 new features.

    Both frames are indexed by bar close time; the join aligns on the
    index. Rows whose 420-bar archive window or 288-bar volcal window
    is not full are NaN in one side and dropped by the caller.
    """
    import pandas as pd

    base = archive_frame[list(BASE_FEATURES)]
    new = frame_new_features(candles)
    frame = pd.concat([base, new], axis=1)
    return frame[list(FEATURES)]


def _masked_features(groups) -> tuple[str, ...]:
    """Feature names for the ablatable volcal groups (mirror of
    archive_training._masked_features). Only the four new groups are
    ablatable: the base candle group is the incumbent's 10 features,
    shared by every volcal design, and is never turned off."""
    if not isinstance(groups, (list, tuple)) or any(
        group not in ("vol", "compression", "volume", "dc") for group in groups
    ):
        raise ValueError("unsupported volcal feature ablation")
    return tuple(FEATURES[i] for group in groups for i in GROUPS[group])


def _check_groups_off(groups) -> None:
    """Reject any ablation outside the four new volcal groups."""
    _masked_features(groups)


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
    masked_features = _masked_features(groups_off)
    parts, coverage_frames = [], {}
    for key, (k, f) in frames.items():
        part = _volcal_frame(k, f)
        part["y"] = build_labels(k, horizon)
        part["stream"] = str(key)
        parts.append(part)
        coverage_frames[key] = (k, f)
    if not parts:
        raise TrainingDataUnavailable("no frames to train on")
    data = pd.concat(parts).sort_index()
    # No optional groups in volcal: every feature must be present.
    data = data.dropna(subset=["y", *FEATURES])
    if masked_features:
        # Ablation (scheduled-grid feature_group_off): the masked
        # groups standardise to 0 via _standardize_apply's NaN -> 0
        # rule, exactly like archive_training. Masking after the drop
        # keeps the full design's training bit-identical.
        data[list(masked_features)] = float("nan")
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
    # No context-absent evaluation: volcal has no context groups, so
    # serving can never lose a context it never reads. The field stays
    # in the evaluation for shape parity with archive_v1 reports.
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
        "serving_capable_without_binance_context": True,
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


def validate(doc: dict) -> dict:
    """Fail closed on any artifact that must not enter the import path."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not a volcal model document")
    if doc.get("family") != FAMILY or doc.get("model_version") != MODEL_VERSION:
        raise ValueError("volcal model identity mismatch")
    if doc.get("feature_contract") != FEATURE_CONTRACT:
        raise ValueError("volcal model feature contract mismatch")
    if tuple(doc.get("features", ())) != FEATURES:
        raise ValueError("volcal model built for different inputs")
    if doc.get("feature_digest") != FEATURE_DIGEST:
        raise ValueError("volcal model feature digest mismatch")
    _check_groups_off(doc.get("feature_groups_off", ()))
    weights = doc.get("weights")
    if not isinstance(weights, list) or len(weights) != len(FEATURES) + 1:
        raise ValueError("volcal model weights have the wrong width")
    if not all(isinstance(w, (int, float)) and math.isfinite(w) for w in weights):
        raise ValueError("volcal model weights are not finite")
    calibration = doc.get("calibration") or {}
    if calibration.get("status") != "evaluated":
        raise ValueError("volcal model calibration did not pass its gate")
    if not all(
        isinstance(calibration.get(k), (int, float)) and math.isfinite(calibration[k])
        for k in ("a", "b")
    ):
        raise ValueError("volcal model calibration parameters are invalid")
    provenance = doc.get("provenance") or {}
    provider = provenance.get("provider_sha256_by_month")
    if not isinstance(provider, dict) or not provider:
        raise ValueError("volcal model provenance lacks provider hashes")
    for month, sha in provider.items():
        if not isinstance(sha, str) or not _SHA64.fullmatch(sha):
            raise ValueError(f"volcal model provider hash for {month} is not a SHA-256")
    normalized = provenance.get("normalized_sha256_by_month")
    if isinstance(normalized, dict):
        # P1-4 lesson: a normalized NDJSON hash must never stand in as a
        # provider hash. Any month whose provider hash equals its normalized
        # hash is refused.
        for month, sha in normalized.items():
            if provider.get(month) == sha:
                raise ValueError(f"volcal model provider hash for {month} is the normalized hash")
    if not isinstance(provenance.get("months_without_provider_hash"), list):
        raise TypeError("volcal model provenance lacks months_without_provider_hash")
    if doc.get("gates_passed") is not True:
        raise ValueError("volcal model did not pass its offline gates")
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


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def probability(doc: dict, vector) -> float:
    """Serve-time probability for a 22-value vector (NaN = group absent).

    Standardised with the artifact's train-only standardiser (missing
    groups contribute the group mean, i.e. standardised 0), Platt
    calibrated, clamped to [0.02, 0.98].
    """
    import numpy as np

    validate(doc)
    if len(vector) != len(FEATURES):
        raise ValueError("wrong volcal feature count")
    mean = np.array(doc["standardizer"]["mean"], dtype=float)
    std = np.array(doc["standardizer"]["std"], dtype=float)
    values = np.array(vector, dtype=float)
    # An ablated artifact never reads its masked groups: NaN them
    # before standardising, exactly like archive_training.probability
    # (their fitted weights are 0 by construction, this is the guard).
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
            artifact=item, batch_id=f"volcal-v1-{item.sha256[:24]}", kind=kind
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
        description="Train the volcal_v1 offline logistic (standalone; the weekly "
        "job runs it through train_weekly_archive instead)."
    )
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
    result = train_run(frames, **training)
    report = dict(result["report"])
    if census is not None:
        report["census"] = census
    out_dir = args.out
    report_path = _write_json(
        out_dir / f"volcal-v1-{args.asset}-{args.horizon}m-report.json", report
    )
    published = {}
    if result["publish_kind"] == "model":
        doc = validate(result["artifact"])
        artifact_path = _write_json(
            out_dir / f"volcal-v1-{args.asset}-{args.horizon}m-model.json", doc
        )
        print(f"volcal-v1 {args.asset} {args.horizon}m: gates PASSED, artifact {artifact_path}")
        if args.publish:
            published = asyncio.run(_publish("model", artifact_path))
    else:
        print(
            f"volcal-v1 {args.asset} {args.horizon}m: gates FAILED "
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
