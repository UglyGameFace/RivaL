"""Optional boosted-tree research on RivaL's validated historical market archive.

Models are trained and selected exclusively on the purged chronological training
and validation periods. The sealed final test is opened for the ONE
validation-selected candidate. No live production candidate, score, order or
promotion may be manufactured from retrospective OHLCV data.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from parlay_bot.perpetuals.markets import HORIZONS
from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT, TARGET_CONTRACT, canonical
from parlay_bot.research.boosted_portable import certified_export
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _metrics, assemble
from parlay_bot.research.live_training import compatible_partitions, historical_features

FAMILIES = ("hist_gradient_boosting", "lightgbm", "xgboost", "catboost")
MAX_TRAIN = 40000
SEED = 420


def _estimators() -> tuple[dict, dict]:
    """Discover optional, bounded CPU-only scientific packages.

    Optional dependencies never execute in the live bot. Missing libraries
    are recorded instead of masquerading as successful evaluations.
    """
    factories = {}
    unavailable = {}
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        factories["hist_gradient_boosting"] = lambda: HistGradientBoostingClassifier(
            max_iter=100, max_leaf_nodes=12, learning_rate=.04,
            l2_regularization=10.0, min_samples_leaf=100,
            early_stopping=False, random_state=SEED,
        )
    except ImportError:
        unavailable["hist_gradient_boosting"] = "dependency_missing"
    try:
        from lightgbm import LGBMClassifier
        factories["lightgbm"] = lambda: LGBMClassifier(
            n_estimators=150, num_leaves=12, max_depth=5,
            learning_rate=.04, min_child_samples=100, reg_lambda=10.0,
            subsample=1.0, colsample_bytree=1.0,
            n_jobs=1, random_state=SEED, verbosity=-1,
        )
    except ImportError:
        unavailable["lightgbm"] = "dependency_missing"
    try:
        from xgboost import XGBClassifier
        factories["xgboost"] = lambda: XGBClassifier(
            n_estimators=150, max_depth=3, learning_rate=.04,
            min_child_weight=40, reg_lambda=10.0,
            n_jobs=1, random_state=SEED, tree_method="hist",
            eval_metric="logloss",
        )
    except ImportError:
        unavailable["xgboost"] = "dependency_missing"
    try:
        from catboost import CatBoostClassifier
        factories["catboost"] = lambda: CatBoostClassifier(
            iterations=150, depth=4, learning_rate=.04,
            l2_leaf_reg=10.0, thread_count=1, random_seed=SEED,
            verbose=False, allow_writing_files=False,
        )
    except ImportError:
        unavailable["catboost"] = "dependency_missing"
    return factories, unavailable


def _scores(model, samples, base):
    predictions = model.predict_proba([x[2] for x in samples])
    scored = []
    for result, sample in zip(predictions, samples, strict=True):
        p = float(result[1])
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid historical model probability")
        scored.append((p, sample[3], sample[0].date().isoformat()))
    return _metrics(scored, base)


def evaluate_compatible_boosters(manifests, *, horizon, prepared=None, estimators=None, artifact_dir=None):
    """Fit a single source-specific offline experiment, never a live model."""
    identity, rows, provenance = assemble(manifests) if prepared is None else prepared[:3]
    vectors = historical_features(rows, identity) if prepared is None else prepared[3]
    train, validation, test = compatible_partitions(
        rows, vectors, horizon=horizon, identity=identity,
    )
    if len(train) < 300 or len(validation) < 100 or len(test) < 100:
        raise HistoryImportError("insufficient purge-separated model research")
    factories, unavailable = _estimators() if estimators is None else (estimators, {})
    if not factories:
        return {"status": "dependencies_missing", "identity": identity,
                "horizon_minutes": horizon, "unavailable": unavailable,
                "live_model_promoted": False}
    # A bounded, temporally spaced (not recency-only) subset retains
    # coverage throughout the original six-month training interval.
    stride = max(1, math.ceil(len(train) / MAX_TRAIN))
    fitted = train[::stride]
    if len({x[3] for x in fitted}) < 2:
        return {"status": "single_class_training", "identity": identity,
                "horizon_minutes": horizon, "live_model_promoted": False}
    base = (sum(s[3] for s in fitted) + 5) / (len(fitted) + 10)
    compared = []
    for family in FAMILIES:
        factory = factories.get(family)
        if factory is None:
            continue
        try:
            model = factory()
            model.fit([s[2] for s in fitted], [s[3] for s in fitted])
            metrics = _scores(model, validation, base)
            compared.append((family, model, metrics))
        except (ValueError, RuntimeError, TypeError, ArithmeticError) as exc:
            unavailable[family] = "training_failed:" + type(exc).__name__
    if not compared:
        return {"status": "no_usable_family", "identity": identity,
                "horizon_minutes": horizon, "unavailable": unavailable,
                "live_model_promoted": False}
    # Strict validation-only selection. Do not inspect the final test for
    # hyperparameter choice or to pick the family that happened to get lucky.
    family, champion, validation_score = min(
        compared, key=lambda x: (x[2]["brier"], x[0]),
    )
    test_score = _scores(champion, test, base)
    try:
        package = {"hist_gradient_boosting": "scikit-learn", "lightgbm": "lightgbm",
                   "xgboost": "xgboost", "catboost": "catboost"}[family]
        package_version = version(package)
    except PackageNotFoundError:
        package_version = "test-double-or-unavailable"
    # Every research identity names the exact known training inputs and
    # library version. It is NOT a weights hash or an importable checkpoint.
    input_digest = hashlib.sha256(canonical([
        [x[0].isoformat(), x[1].isoformat(), x[2], x[3]] for x in fitted
    ]).encode()).hexdigest()
    spec = {
        "model": "boosted-offline-research-v1", "family": family,
        "training_input_sha256": input_digest,
        "package_version": package_version,
        "identity": identity, "horizon_minutes": horizon,
        "feature_contract": FEATURE_CONTRACT, "target_contract": TARGET_CONTRACT,
        "source_sha256_by_month": provenance["source_sha256_by_month"],
        "training_count": len(fitted),
        "train_cutoff": train[-1][1].isoformat(),
        "validation_cutoff": test[0][0].isoformat(),
        "evaluation_completed_through": test[-1][1].isoformat(),
        "model_serialized": False,
    }
    passed = (len(provenance["months"]) >= 6
              and all(m["brier_gain"] > .005
                      and m["ece_10"] <= .10 and m["days"] >= 7
                      for m in (validation_score, test_score)))
    portable_candidates = []
    if artifact_dir is not None:
        # Export all independent native tree families after validation choice.
        # The final test was not used for model selection. Every prospective
        # challenger must pass independent future outcome and cost proof.
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for name, trained_model, val_score in compared:
            if name not in ("lightgbm", "xgboost", "catboost"):
                continue
            test_metrics = _scores(trained_model, test, base)
            try:
                held_vectors = [row[2] for row in validation[:64]]
                portable, parity_error = certified_export(trained_model, name, held_vectors)
                portable_spec = {
                    "model": "portable-boosted-binary-v1",
                    "family": name, "identity": identity,
                    "horizon_minutes": horizon,
                    "feature_contract": FEATURE_CONTRACT,
                    "target_contract": TARGET_CONTRACT,
                    "portable_model": portable,
                    "native_parity_max_error": parity_error,
                    "training_input_sha256": input_digest,
                    "training_count": len(fitted),
                    "train_cutoff": train[-1][1].isoformat(),
                    "validation_cutoff": test[0][0].isoformat(),
                    "evaluation_completed_through": test[-1][1].isoformat(),
                    "source_sha256_by_month": provenance["source_sha256_by_month"],
                }
                candidate_sha = hashlib.sha256(canonical(portable_spec).encode()).hexdigest()
                artifact = {
                    "research_only": True, "live_model_promoted": False,
                    "model_sha256": candidate_sha, "model_spec": portable_spec,
                    "validation": val_score, "out_of_sample_test": test_metrics,
                }
                data = canonical(artifact).encode()
                if len(data) > 2_000_000:
                    raise ValueError("portable artifact too large")
                name_part = identity["venue"] + "-" + identity["contract"]
                file = directory / f"boosted-{name_part}-{horizon}-{name}-{candidate_sha[:20]}.json"
                file.write_bytes(data)
                portable_candidates.append({
                    "family": name, "model_sha256": candidate_sha,
                    "path": str(file), "size": len(data),
                    "native_parity_max_error": parity_error,
                    "retrospective_passed": (
                        len(provenance["months"]) >= 6
                        and all(m["brier_gain"] > .005 and m["ece_10"] <= .10
                                and m["days"] >= 7
                                for m in (val_score, test_metrics))
                    ),
                })
            except (ValueError, TypeError, KeyError, IndexError, OverflowError,
                    AttributeError) as exc:
                unavailable[name + "_portable_export"] = type(exc).__name__
    return {
        "portable_candidates": portable_candidates,
        "schema_version": "rival-boosted-tree-research-v1",
        "status": "evaluated",
        "identity": identity, "horizon_minutes": horizon,
        "model_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "model_spec": spec,
        "validation": validation_score, "out_of_sample_test": test_score,
        "compared_on_validation": [
            {"family": name, "scores": metrics} for name, _, metrics in compared
        ],
        "unavailable": unavailable,
        "research_candidate_passed": passed,
        "live_model_promoted": False, "prospective_accuracy": None,
        "eligible_for_live_import": False,
        "reason": "historical tree model is not a signed, source-compatible live artifact",
    }


def evaluate_directory(root: Path, *, qualified_streams, artifact_dir=None):
    groups = defaultdict(list)
    for path in sorted((root / "binance_um_klines").glob("*.manifest.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        key = (meta.get("contract"), meta.get("interval_minutes"))
        if key[1] == 5 and key in qualified_streams:
            groups[key].append(path)
    results = []
    for paths in groups.values():
        identity, rows, provenance = assemble(paths)
        vectors = historical_features(rows, identity)
        prepared = (identity, rows, provenance, vectors)
        for horizon in HORIZONS:
            try:
                results.append(evaluate_compatible_boosters(
                    paths, horizon=horizon, prepared=prepared, artifact_dir=artifact_dir,
                ))
            except HistoryImportError:
                continue
    return results
