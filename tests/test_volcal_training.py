"""volcal_v1 offline training: gates, validation, horizon independence."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from parlay_bot.perpetuals.volcal_model import VolcalModelSet, model_sha256
from parlay_bot.research import archive_features, volcal_training
from parlay_bot.research.volcal_features import FEATURE_DIGEST, FEATURES
from parlay_bot.storage.hot import SQLiteHotStore

START = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 7, 12, 0, 30, tzinfo=UTC)


def _frames(n=6000, rho=0.5, seed=7):
    """One BTC stream whose log-returns are AR(1): ret_1 predicts the
    next bar, so a learnable signal exists when rho is large."""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 0.002, size=n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = rho * r[i - 1] + eps[i]
    closes = 100.0 * np.exp(np.cumsum(r))
    index = pd.DatetimeIndex([START + timedelta(minutes=5 * i) for i in range(n)])
    k = pd.DataFrame({
        "open": closes, "high": closes * 1.002, "low": closes * 0.998,
        "close": closes, "volume": 10.0, "trades": 3,
    }, index=index)
    return {"bitget|BTC": (k, archive_features.frame_features(k, None, "BTC"))}


def _provenance():
    return {
        "provider_sha256_by_month": {"bitget:2026-01": "a" * 64},
        "months_without_provider_hash": [],
        "manifest_sha256_by_month": {},
    }


def _run(frames, *, horizon=5, **over):
    k = next(iter(frames.values()))[0]
    n = len(k)
    kwargs = {
        "asset": "BTC", "horizon": horizon,
        "train_end": k.index[int(0.66 * n)], "test_start": k.index[int(0.83 * n)],
        "trained_through": k.index[-1],
        "provenance": _provenance(), "code_version": "test",
    }
    kwargs.update(over)
    return volcal_training.train_run(frames, **kwargs)


def _valid_doc(*, asset="BTC", horizon=5, trained_through=None, weights=None):
    trained_through = trained_through or (NOW - timedelta(days=1))
    weights = (
        weights if weights is not None
        else [0.01 * ((i % 7) - 3) for i in range(23)]
    )
    return {
        "schema": "rival-volcal-model-v1",
        "family": "volcal_v1",
        "model_version": "volcal-offline-v1",
        "asset": asset,
        "horizon_minutes": horizon,
        "feature_contract": "volcal-candle-vol-v1",
        "features": list(FEATURES),
        "feature_digest": FEATURE_DIGEST,
        "weights": list(weights),
        "standardizer": {"mean": [0.0] * 22, "std": [1.0] * 22},
        "calibration": {"method": "platt", "status": "evaluated", "a": 1.0,
                        "b": 0.0, "ece": 0.02, "fit_rows": 200, "eval_rows": 100,
                        "top_bucket_hit_rate": 0.6, "top_bucket_n": 20,
                        "top_bucket_mean_confidence": 0.6},
        "trained_from": "2026-01-01T00:00:00+00:00",
        "trained_through": trained_through.isoformat(),
        "training_rows": 1000,
        "gates_passed": True,
        "evaluation_status": "held-out evaluated",
        "provenance": {
            "provider_sha256_by_month": {"bitget:2026-01": "a" * 64},
            "normalized_sha256_by_month": {"bitget:2026-01": "b" * 64},
            "months_without_provider_hash": [],
        },
        "created_at": trained_through.isoformat(),
    }


def test_train_run_passes_gates_on_learnable_signal():
    result = _run(_frames(rho=0.5), horizon=5)
    assert result["publish_kind"] == "model"
    assert result["report"]["gates_passed"] is True
    doc = volcal_training.validate(result["artifact"])
    assert doc["family"] == "volcal_v1"
    assert doc["horizon_minutes"] == 5
    assert len(doc["weights"]) == 23
    assert doc["evaluation"]["context_absent_test"] is None
    assert doc["serving_capable_without_binance_context"] is True
    p = volcal_training.probability(doc, [0.0] * 22)
    assert 0.02 <= p <= 0.98


def test_train_run_reports_only_on_noise():
    result = _run(_frames(rho=0.0), horizon=5)
    assert result["publish_kind"] == "report"
    assert result["artifact"] is None
    assert result["report"]["gates_passed"] is False
    assert result["report"]["gate_failures"]  # random data clears no bar


def test_train_run_rejects_small_splits():
    frames = _frames(n=900)
    with pytest.raises(volcal_training.TrainingDataUnavailable):
        _run(frames, horizon=5)


def test_train_run_rejects_feature_ablation():
    with pytest.raises(ValueError):
        _run(_frames(), horizon=5, groups_off=("premium",))


def test_validate_rejects_bad_artifacts():
    good = _valid_doc()
    volcal_training.validate(good)
    bad = _valid_doc()
    bad["feature_contract"] = "volcal-candle-vol-v2"
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    bad = _valid_doc()
    bad["weights"] = bad["weights"][:-1]  # wrong width
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    bad = _valid_doc()
    bad["weights"][0] = float("nan")
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    bad = _valid_doc()
    bad["calibration"]["status"] = "unevaluated"
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    bad = _valid_doc()
    bad["gates_passed"] = False
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    bad = _valid_doc()
    bad["provenance"]["normalized_sha256_by_month"] = {"bitget:2026-01": "a" * 64}
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    bad = _valid_doc()
    bad["provenance"]["provider_sha256_by_month"] = {}
    with pytest.raises(ValueError):
        volcal_training.validate(bad)
    assert not volcal_training.usable(_valid_doc(trained_through=NOW + timedelta(days=1)), now=NOW)


def test_probability_clamps_to_serve_range():
    doc = _valid_doc(weights=[100.0] + [100.0] * 22)
    assert volcal_training.probability(doc, [1.0] * 22) == pytest.approx(0.98)
    doc = _valid_doc(weights=[-100.0] + [-100.0] * 22)
    assert volcal_training.probability(doc, [1.0] * 22) == pytest.approx(0.02)
    with pytest.raises(ValueError):
        volcal_training.probability(_valid_doc(), [0.0] * 21)


def test_five_minute_fit_is_independent_of_fifteen_minute(tmp_path):
    """A 5-minute model must not read 15-minute params: same frames,
    different horizons, different fitted weights and different stored
    artifacts (the borrowed-15-min-confidence defect, ruled out)."""
    frames = _frames(rho=0.5)
    doc5 = volcal_training.validate(_run(frames, horizon=5)["artifact"])
    doc15 = volcal_training.validate(_run(frames, horizon=15)["artifact"])
    assert doc5["horizon_minutes"] == 5
    assert doc15["horizon_minutes"] == 15
    assert doc5["weights"] != doc15["weights"]
    assert model_sha256(doc5) != model_sha256(doc15)
    store = SQLiteHotStore(tmp_path / "volcal.sqlite")
    try:
        model_set = VolcalModelSet(store)
        model_set.import_document("file-5", json.dumps(doc5).encode())
        model_set.import_document("file-15", json.dumps(doc15).encode())
        active5 = model_set.active("BTC", 5, now=NOW)
        active15 = model_set.active("BTC", 15, now=NOW)
        assert active5 is not None and active15 is not None
        assert active5["_sha256"] != active15["_sha256"]
        assert active5["horizon_minutes"] == 5
        assert active15["horizon_minutes"] == 15
    finally:
        store.close()
