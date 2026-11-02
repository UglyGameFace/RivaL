"""Source-native training never mixes identical ticker names from venues."""
from __future__ import annotations

import json

import pytest

from parlay_bot.research import boosted_models, live_training
from parlay_bot.research.source_native_offline import screening_receipt
from tests.test_research_candidate_registry import artifact


def _manifest(root, *, source, venue, contract, quote, month):
    folder = root / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{venue}-{contract}-{quote}-{month}.manifest.json"
    path.write_text(json.dumps({
        "source": source,
        "venue": venue,
        "contract": contract,
        "quote": quote,
        "interval_minutes": 5,
        "month": month,
    }), encoding="utf-8")
    return path


def test_both_trainers_preserve_exact_venue_contract_quote_source(tmp_path, monkeypatch):
    # Same BTC contract and timestamp across exchanges must not be assembled
    # as one synthetic exchange or make each other's training fail.
    requested = {
        ("hyperliquid", "BTC", "USDC", 5),
        ("dydx", "BTC", "USDC", 5),
        ("hyperliquid", "BTC", "USDT", 5),
        ("BTCUSDT", 5),  # original Binance stays a separate source
    }
    for source, venue, contract, quote in (
        ("verified_venue_perp_klines", "hyperliquid", "BTC", "USDC"),
        ("verified_venue_perp_klines", "dydx", "BTC", "USDC"),
        ("verified_venue_perp_klines", "hyperliquid", "BTC", "USDT"),
        ("verified_venue_perp_klines", "okx", "BTC", "USDC"),
        ("binance_um_klines", "binance", "BTCUSDT", "USDT"),
    ):
        for month in ("2026-04", "2026-05"):
            _manifest(tmp_path, source=source, venue=venue, contract=contract,
                      quote=quote, month=month)
    groups = live_training.qualified_manifest_groups(
        tmp_path, qualified_streams=requested,
    )
    assert len(groups) == 4
    assert all(len(group) == 2 for group in groups)

    def fake_assemble(paths):
        metadata = [json.loads(path.read_text()) for path in paths]
        names = {
            (m["source"], m["venue"], m["contract"], m["quote"],
             m["interval_minutes"]) for m in metadata
        }
        assert len(names) == 1, "cross-venue model history would be contaminated"
        meta = metadata[0]
        return ({
            k: meta[k] for k in ("venue", "contract", "quote", "interval_minutes")
        }, [], {"months": ["2026-04", "2026-05"]})

    def fake_train(paths, *, horizon, prepared):
        return {
            "venue": prepared[0]["venue"],
            "contract": prepared[0]["contract"],
            "quote": prepared[0]["quote"],
            "horizon": horizon,
        }

    for module, evaluator in (
        (live_training, "evaluate_compatible"),
        (boosted_models, "evaluate_compatible_boosters"),
    ):
        monkeypatch.setattr(module, "assemble", fake_assemble)
        monkeypatch.setattr(module, "historical_features",
                            lambda rows, identity: {})
        monkeypatch.setattr(
            module, evaluator,
            (lambda paths, *, horizon, prepared, artifact_dir=None:
             fake_train(paths, horizon=horizon, prepared=prepared)),
        )
    logistic = live_training.evaluate_directory(
        tmp_path, qualified_streams=requested,
    )
    boosted = boosted_models.evaluate_directory(
        tmp_path, qualified_streams=requested,
    )
    expected = {
        ("hyperliquid", "BTC", "USDC"),
        ("dydx", "BTC", "USDC"),
        ("hyperliquid", "BTC", "USDT"),
        ("binance", "BTCUSDT", "USDT"),
    }
    for rows in (logistic, boosted):
        assert len(rows) == 4 * len(live_training.HORIZONS)
        assert {(r["venue"], r["contract"], r["quote"]) for r in rows} == expected
        assert all(r["venue"] != "okx" for r in rows)


def test_native_publisher_screening_matches_actual_importer_and_rejects_weak_model():
    document = json.loads(artifact())
    item = {
        "model_sha256": document["model_sha256"],
        "model_spec": document["model_spec"],
        "validation": document["validation"],
        "out_of_sample_test": document["out_of_sample_test"],
        "research_candidate_passed": True,
    }
    report = screening_receipt([item], [])
    assert report["evaluated"] == 1
    assert report["retrospective_passed"] == 1
    assert report["source_specific_outcomes"][0]["venue"] == "binance"
    assert not report["proves_prospective_edge"]

    item["validation"] = {**item["validation"], "brier_gain": .001}
    item["research_candidate_passed"] = False
    rejected = screening_receipt([item], [])
    assert rejected["retrospective_passed"] == 0
    assert rejected["rejected_by_reason"] == [
        {"reason": "validation_brier_gain_below_0.005", "count": 1},
    ]
    item["research_candidate_passed"] = True
    from parlay_bot.research.free_history import HistoryImportError
    with pytest.raises(HistoryImportError, match="screening disagree"):
        screening_receipt([item], [])


def test_native_publisher_blocks_corrupted_candidate_before_drive_upload():
    doc = json.loads(artifact())
    wrong = {
        "model_sha256": "f" * 64,
        "model_spec": doc["model_spec"],
        "validation": doc["validation"],
        "out_of_sample_test": doc["out_of_sample_test"],
        "research_candidate_passed": True,
    }
    from parlay_bot.research.free_history import HistoryImportError
    with pytest.raises(HistoryImportError, match="importer contract"):
        screening_receipt([wrong], [])
