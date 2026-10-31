"""Portable, dependency-free inference for narrowly verified boosted research trees.

Only numeric ten-feature binary models are supported. Native library output must
agree on held-out feature vectors BEFORE exporting an artifact. No pickle, code
execution, cross-venue weight transfer, or automatic trade authorization.
"""
from __future__ import annotations

import json
import math
import re
import struct
import tempfile
from pathlib import Path

FAMILIES = ("lightgbm", "xgboost", "catboost")
MAX_TREES = 300
MAX_NODES = 30000
MAX_BYTES = 2_000_000


def _finite(number):
    if type(number) not in (int, float) or not math.isfinite(number):
        raise ValueError("non-finite boosted model value")
    return float(number)


def _node_lgb(node):
    if "leaf_value" in node:
        return {"value": _finite(node["leaf_value"])}
    if node.get("decision_type") != "<=":
        raise ValueError("unsupported LightGBM categorical split")
    return {
        "feature": int(node["split_feature"]), "threshold": _finite(node["threshold"]),
        "op": "le", "missing_left": bool(node["default_left"]),
        "left": _node_lgb(node["left_child"]), "right": _node_lgb(node["right_child"]),
    }


def _node_xgb(node):
    if "leaf" in node:
        return {"value": _finite(node["leaf"])}
    match = re.fullmatch(r"f([0-9]+)", str(node.get("split", "")))
    if not match:
        raise ValueError("unsupported XGBoost split")
    children = {int(child["nodeid"]): child for child in node["children"]}
    yes, no = int(node["yes"]), int(node["no"])
    return {
        "feature": int(match.group(1)), "threshold": _finite(node["split_condition"]),
        "op": "lt", "missing_left": int(node["missing"]) == yes,
        "left": _node_xgb(children[yes]), "right": _node_xgb(children[no]),
    }


def export_portable(model, family):
    """Convert native, numerical binary classifiers to auditable data only."""
    if family == "lightgbm":
        native = model.booster_.dump_model()
        if native.get("objective", "").split()[0] != "binary":
            raise ValueError("only LightGBM binary objective is supported")
        trees = [_node_lgb(item["tree_structure"]) for item in native["tree_info"]]
        portable = {"family": family, "kind": "branch", "base_margin": 0.0,
                    "sigmoid_scale": 1.0, "trees": trees}
    elif family == "xgboost":
        booster = model.get_booster()
        raw = json.loads(booster.save_raw(raw_format="json"))
        learner = raw["learner"]
        objective = learner["objective"]["name"]
        if objective != "binary:logistic":
            raise ValueError("only XGBoost logistic objective is supported")
        raw_base = learner["learner_model_param"]["base_score"]
        if isinstance(raw_base, str) and raw_base.startswith("["):
            raw_base = json.loads(raw_base)[0]
        base = _finite(float(raw_base))
        if not 0 < base < 1:
            raise ValueError("invalid XGBoost base rate")
        trees = [_node_xgb(json.loads(item)) for item in
                 booster.get_dump(dump_format="json")]
        portable = {"family": family, "kind": "branch",
                    "base_margin": math.log(base/(1-base)),
                    "sigmoid_scale": 1.0, "trees": trees}
    elif family == "catboost":
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catboost_model.json"
            model.save_model(str(target), format="json")
            native = json.loads(target.read_text(encoding="utf-8"))
        scale, bias = native.get("scale_and_bias", [1.0, [0.0]])
        if type(bias) is not list or len(bias) != 1:
            raise ValueError("unsupported CatBoost output dimensionality")
        trees = []
        for tree in native["oblivious_trees"]:
            splits = []
            for item in tree["splits"]:
                if item.get("split_type") != "FloatFeature":
                    raise ValueError("CatBoost categorical models are not portable")
                splits.append({
                    "feature": int(item["float_feature_index"]),
                    "threshold": _finite(item["border"]),
                })
            trees.append({"splits": splits,
                          "values": [_finite(v) for v in tree["leaf_values"]]})
        portable = {"family": family, "kind": "oblivious", "trees": trees,
                    "scale": _finite(scale), "bias": _finite(bias[0])}
    else:
        raise ValueError("unsupported boosted family")
    validate_portable(portable)
    return portable


def validate_portable(portable):
    """Bound model complexity and reject unsupported or corrupt tree structures."""
    if not isinstance(portable, dict) or portable.get("family") not in FAMILIES:
        raise ValueError("unknown portable model")
    trees = portable.get("trees")
    if not isinstance(trees, list) or not 1 <= len(trees) <= MAX_TREES:
        raise ValueError("invalid boosted tree count")
    if len(json.dumps(portable, allow_nan=False, separators=(",", ":"))) > MAX_BYTES:
        raise ValueError("oversized boosted tree model")
    count = 0

    def branch(node, depth):
        nonlocal count
        count += 1
        if depth > 18 or count > MAX_NODES or not isinstance(node, dict):
            raise ValueError("invalid boosted tree depth or size")
        if set(node) == {"value"}:
            _finite(node["value"])
            return
        if set(node) != {"feature", "threshold", "op", "missing_left", "left", "right"}:
            raise ValueError("unsupported boosted branch")
        if type(node["feature"]) is not int or not 0 <= node["feature"] < 10:
            raise ValueError("out-of-scope boosted feature")
        if node["op"] not in ("lt", "le") or type(node["missing_left"]) is not bool:
            raise ValueError("unsupported boosted split")
        _finite(node["threshold"])
        branch(node["left"], depth + 1)
        branch(node["right"], depth + 1)

    if portable["family"] in ("lightgbm", "xgboost"):
        if portable.get("kind") != "branch" or portable.get("sigmoid_scale") != 1.0:
            raise ValueError("unsupported boosted link")
        _finite(portable["base_margin"])
        for tree in trees:
            branch(tree, 0)
    else:
        if portable.get("kind") != "oblivious":
            raise ValueError("unsupported CatBoost format")
        _finite(portable["scale"])
        _finite(portable["bias"])
        for tree in trees:
            count += 1
            splits = tree.get("splits")
            leaves = tree.get("values")
            if not isinstance(splits, list) or not 0 <= len(splits) <= 12:
                raise ValueError("invalid CatBoost depth")
            if not isinstance(leaves, list) or len(leaves) != 2 ** len(splits):
                raise ValueError("invalid CatBoost leaves")
            for split in splits:
                if (not isinstance(split, dict) or set(split) != {"feature", "threshold"}
                    or type(split["feature"]) is not int
                    or not 0 <= split["feature"] < 10):
                    raise ValueError("invalid CatBoost feature")
                _finite(split["threshold"])
            for leaf in leaves:
                _finite(leaf)
            count += len(leaves)
            if count > MAX_NODES:
                raise ValueError("oversized boosted leaf model")
    return portable


def predict_portable(portable, features, *, trusted=False):
    """Return a calibrated-format probability, not a calibrated *claim*."""
    if not trusted:
        validate_portable(portable)
    if len(features) != 10 or any(
        type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 5
        for v in features
    ):
        raise ValueError("invalid ten-feature live input")
    if portable["kind"] == "branch":
        total = portable["base_margin"]
        for tree in portable["trees"]:
            node = tree
            while "value" not in node:
                value = features[node["feature"]]
                # XGBoost quantizes inference inputs to float32. Comparing
                # the original Python float64 at an exact split boundary
                # can choose the wrong leaf, even if tree weights match.
                if portable["family"] == "xgboost":
                    value = struct.unpack("<f", struct.pack("<f", value))[0]
                left = (value < node["threshold"] if node["op"] == "lt"
                        else value <= node["threshold"])
                node = node["left"] if left else node["right"]
            total += node["value"]
    else:
        total = 0.0
        for tree in portable["trees"]:
            index = 0
            for depth, split in enumerate(tree["splits"]):
                index |= int(features[split["feature"]] > split["threshold"]) << depth
            total += tree["values"][index]
        total = portable["scale"] * total + portable["bias"]
    return max(1e-9, min(1 - 1e-9, 1/(1+math.exp(-max(-50., min(50., total))))))


def certified_export(model, family, validation_features):
    """Fail closed unless native and portable predictions match on holdout inputs."""
    if not validation_features:
        raise ValueError("no parity samples")
    portable = export_portable(model, family)
    expected = model.predict_proba(validation_features)
    error = max(abs(predict_portable(portable, features, trusted=True)-float(row[1]))
                for features, row in zip(validation_features, expected, strict=True))
    if not math.isfinite(error) or error > 1e-6:
        raise ValueError("native and portable inference disagree")
    return portable, error
