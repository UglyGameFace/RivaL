"""Train/serve parity and inference without offline scientific dependencies."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from parlay_bot.research import (
    archive_training,
    regime2_training,
    volcal_training,
    xmarket_training,
)
from tests.test_archive_v1_shadow import _valid_doc as archive_doc
from tests.test_regime_v2 import _valid_regime2_doc as regime_doc
from tests.test_volcal_v1_shadow import _valid_doc as volcal_doc
from tests.xmarket_fixtures import valid_doc as xmarket_doc

FAMILIES = (
    (archive_training, archive_doc),
    (volcal_training, volcal_doc),
    (regime2_training, regime_doc),
    (xmarket_training, xmarket_doc),
)


def _training_reference(module, doc, vector):
    values = np.array(vector, dtype=float)
    if module in (archive_training, volcal_training):
        for name in module._masked_features(doc.get("feature_groups_off", ())):
            values[doc["features"].index(name)] = np.nan
    x = archive_training._standardize_apply(
        values, np.array(doc["standardizer"]["mean"]), np.array(doc["standardizer"]["std"]))
    raw = archive_training._raw_probabilities(np.array(doc["weights"]), x[None, :])
    return float(archive_training._platt_apply(
        doc["calibration"]["a"], doc["calibration"]["b"], raw)[0])


@pytest.mark.parametrize("module,factory", FAMILIES, ids=lambda v: getattr(v, "__name__", ""))
def test_scalar_serving_matches_training_numpy_transform(module, factory):
    rng = np.random.default_rng(20261114)
    for case in range(64):
        doc = factory()
        width = len(doc["features"])
        doc["weights"] = rng.normal(0, .5, width + 1).tolist()
        doc["weights_sha256"] = hashlib.sha256(json.dumps(
            doc["weights"], separators=(",", ":")).encode()).hexdigest()
        doc["standardizer"] = {"mean": rng.normal(0, 2, width).tolist(),
                               "std": rng.uniform(.01, 3, width).tolist()}
        doc["calibration"].update(a=.7 + case / 30, b=-.4)
        vector = rng.normal(0, 20, width).tolist()
        if module is not xmarket_training and case % 2:
            vector[:3] = [math.nan, math.inf, -math.inf]
        if case < 2:
            doc["weights"][0] = (-1 if case else 1) * 1000
            doc["weights_sha256"] = hashlib.sha256(json.dumps(
                doc["weights"], separators=(",", ":")).encode()).hexdigest()
        expected = _training_reference(module, doc, vector)
        assert module.probability(doc, vector) == pytest.approx(expected, abs=1e-12, rel=1e-12)


@pytest.mark.parametrize("module,factory,groups", [
    (archive_training, archive_doc, ["premium", "binance_tape"]),
    (volcal_training, volcal_doc, ["vol", "volume"]),
])
def test_ablation_masks_stay_absent_before_standardization(module, factory, groups):
    doc = factory()
    doc["feature_groups_off"] = groups
    vector = [float(n) / 10 for n in range(len(doc["features"]))]
    expected = _training_reference(module, doc, vector)
    assert module.probability(doc, vector) == pytest.approx(expected, abs=1e-12)
    for name in module._masked_features(groups):
        vector[doc["features"].index(name)] = 1e100
    assert module.probability(doc, vector) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("module,factory", FAMILIES, ids=lambda v: getattr(v, "__name__", ""))
def test_serving_in_isolated_python_without_site_packages(module, factory):
    doc = factory()
    vector = [.125] * len(doc["features"])
    if module is not xmarket_training:
        vector[0] = math.nan
    expected = _training_reference(module, doc, vector)
    script = """
import importlib
import importlib.util
import json
import sys
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec("numpy") is None
assert importlib.util.find_spec("pandas") is None
doc, vector = json.load(sys.stdin)
module = importlib.import_module(sys.argv[2])
print(module.probability(doc, vector))
assert "numpy" not in sys.modules and "pandas" not in sys.modules
"""
    run = subprocess.run([sys.executable, "-I", "-S", "-c", script,
                          str(Path(__file__).resolve().parents[1] / "src"), module.__name__],
                         input=json.dumps([doc, vector]), text=True,
                         capture_output=True, timeout=20, check=True)
    assert float(run.stdout) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_xmarket_still_rejects_nonfinite_required_inputs(value):
    doc = xmarket_doc()
    vector = [0.] * len(doc["features"])
    vector[0] = value
    with pytest.raises(ValueError, match="feature count"):
        xmarket_training.probability(doc, vector)
