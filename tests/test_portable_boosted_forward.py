"""Source-compatible boosted tree artifacts and genuine future-only shadow guards."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.research.boosted_portable import (
    certified_export, predict_portable, validate_portable,
)
from parlay_bot.research.candidate_registry import (
    CandidateValidationError, ResearchCandidateRegistry, verified_candidate,
)


def _tree():
    return {
        "family": "lightgbm", "kind": "branch", "base_margin": 0.0,
        "sigmoid_scale": 1.0,
        "trees": [{
            "feature": 0, "threshold": 0.0, "op": "le",
            "missing_left": True,
            "left": {"value": -1.0}, "right": {"value": 1.0},
        }],
    }


def _document():
    spec = {
        "model": "portable-boosted-binary-v1", "family": "lightgbm",
        "identity": {"venue": "binance", "contract": "BTCUSDT",
                     "quote": "USDT", "interval_minutes": 5},
        "horizon_minutes": 5,
        "feature_contract": "relative-rsi-macd-bollinger-atr-volume-40x5m-v1",
        "target_contract": "future-5m-close-above-reference-close-with-horizon-delay-v1",
        "portable_model": _tree(), "native_parity_max_error": 0.0,
        "training_input_sha256": "b" * 64,
        "training_count": 1800,
        "train_cutoff": "2026-04-01T00:00:00+00:00",
        "validation_cutoff": "2026-05-01T00:00:00+00:00",
        "evaluation_completed_through": "2026-06-01T00:00:00+00:00",
        "source_sha256_by_month": {
            f"2026-{month:02d}": hashlib.sha256(str(month).encode()).hexdigest()
            for month in range(1, 7)
        },
    }
    sha = hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    metric = {
        "count": 200, "brier_gain": 0.02, "ece_10": .04, "days": 10,
        "accuracy": .60, "brier": .23, "baseline_brier": .25,
    }
    return {
        "research_only": True, "live_model_promoted": False,
        "model_sha256": sha, "model_spec": spec,
        "validation": metric, "out_of_sample_test": metric,
    }


def _bytes(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def _samples(now):
    result = []
    for i in range(130):
        issued = now-timedelta(minutes=5*(140-i))
        result.append(LabeledExample(
            venue="binance", contract="BTCUSDT", quote="USDT",
            horizon_minutes=5, issued_at=issued,
            label_known_at=issued+timedelta(minutes=5),
            features=tuple(float((i+j)%7-3)/3 for j in range(10)),
            label_up=int(i%3 == 0),
        ))
    return result


def test_portable_tree_prediction_and_reject_unsafe_nodes():
    model = _tree()
    assert predict_portable(model, (-.2,) + (0.,)*9) < .5
    assert predict_portable(model, (.2,) + (0.,)*9) > .5
    with pytest.raises(ValueError):
        predict_portable(model, (float("nan"),) + (0.,)*9)
    model["trees"][0]["feature"] = 12
    with pytest.raises(ValueError, match="out-of-scope"):
        validate_portable(model)
    with pytest.raises(ValueError):
        predict_portable(model, (0.,)*10)


def test_tampered_or_cross_contract_historical_tree_is_never_eligible():
    doc = _document()
    assert verified_candidate(_bytes(doc))["retrospective_screen"] == "passed"
    doc["model_spec"]["portable_model"]["trees"][0]["right"]["value"] = 20
    with pytest.raises(CandidateValidationError):
        verified_candidate(_bytes(doc))
    doc = _document()
    doc["model_spec"]["identity"]["venue"] = "hyperliquid"
    with pytest.raises(CandidateValidationError):
        verified_candidate(_bytes(doc))
    doc = _document()
    doc["model_spec"]["native_parity_max_error"] = .5
    with pytest.raises(CandidateValidationError):
        verified_candidate(_bytes(doc))
    doc = _document()
    doc["live_model_promoted"] = True
    with pytest.raises(CandidateValidationError):
        verified_candidate(_bytes(doc))


def test_exact_source_boosted_artifact_issues_immutable_future_paper_forecast(
    learning_store,
):
    hot = learning_store
    market = PerpetualMarketStore(hot)
    ForwardAccuracyMonitor(market).initialize()
    registry = ResearchCandidateRegistry(hot)
    registry.initialize()
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    registry.import_verified(
        "boostedEvidence1", _bytes(_document()), now=now-timedelta(days=20),
    )
    same = registry.compatible_boosters(
        venue="binance", contract="BTCUSDT", quote="USDT", horizon=5, now=now,
    )
    assert len(same) == 1
    assert registry.compatible_boosters(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon=5, now=now,
    ) == []
    assert registry.compatible_candidates(
        venue="binance", contract="BTCUSDT", quote="USDT", horizon=5, now=now,
    ) == []
    comp = ShadowModelCompetition(hot)
    issued = now-timedelta(seconds=20)
    target = now+timedelta(minutes=5)
    arguments = dict(
        conn=None, samples=_samples(now), features=(.25,)*10, now=now,
        venue="binance", contract="BTCUSDT", quote="USDT", asset="BTC",
        horizon=5, reference_opened_at=now-timedelta(minutes=5),
        target_opened_at=target, issued_at=issued,
        incumbent_up=.6, baseline_up=.45, boosters=same,
    )
    with hot.connect() as conn:
        arguments["conn"] = conn
        assert comp.issue(**arguments) == 3
        assert comp.issue(**arguments) == 0
        rows = conn.execute(
            "SELECT family,training_sha256,settled_at FROM rival_model_shadow_forecasts"
        ).fetchall()
    boosted = [row for row in rows if row["family"].startswith("boosted_")]
    assert len(boosted) == 1
    assert boosted[0]["training_sha256"] == same[0]["model_sha256"]
    assert boosted[0]["settled_at"] is None
    assert comp.settle(now=now+timedelta(minutes=8)) == 0
    governance = FamilyPromotion(hot).evaluate(
        asset="BTC", venue="binance", contract="BTCUSDT",
        quote="USDT", horizon=5, now=now,
    )
    assert governance["status"] == "held"
    assert governance["selected_family"] is None
    assert governance["families"][boosted[0]["family"]]["qualified"] is False
    assert not governance["real_fills_verified"]


def test_catboost_oblivious_leaves_have_explicit_direction():
    model = {
        "family": "catboost", "kind": "oblivious",
        "scale": 1.0, "bias": 0.0,
        "trees": [{"splits": [{"feature": 0, "threshold": .1}],
                   "values": [-1., 1.]}],
    }
    assert predict_portable(model, (0.,)*10) < .5
    assert predict_portable(model, (1.,) + (0.,)*9) > .5
    model["trees"][0]["values"] = [1.]
    with pytest.raises(ValueError):
        validate_portable(model)
