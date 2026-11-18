from __future__ import annotations

import copy

import pytest

from parlay_bot.research import xmarket_training as training
from tests.xmarket_fixtures import frames_and_inputs, valid_doc


def run(frames, inputs, *, horizon=5, groups_off=()):
    k = next(iter(frames.values()))[0]
    return training.train_run(frames, asset="BTC", horizon=horizon,
        train_end=k.index[int(len(k) * .66)], test_start=k.index[int(len(k) * .83)],
        trained_through=k.index[-1], code_version="test", market_data=inputs,
        groups_off=groups_off,
        provenance={"provider_sha256_by_month": {"bitget:2026-01": "a" * 64},
                    "months_without_provider_hash": []})


def test_fit_uses_own_labels_complete_overlap_and_held_out_calibration(monkeypatch):
    from parlay_bot.perpetuals import flow_challenger_v3
    seen = []
    original = flow_challenger_v3.fit_platt
    def spy(fit_rows, eval_rows):
        seen.append((len(fit_rows), len(eval_rows)))
        return original(fit_rows, eval_rows)
    monkeypatch.setattr(flow_challenger_v3, "fit_platt", spy)
    frames, inputs = frames_and_inputs()
    result = run(frames, inputs)
    assert result["publish_kind"] == "model", result["report"].get("gate_failures")
    doc = training.validate(result["artifact"])
    assert doc["input_count"] == 18
    assert doc["horizon_minutes"] == 5
    assert doc["cross_horizon_input"] is True
    assert doc["native_horizon_minutes"] == 15
    assert doc["calibration"]["fit_rows"] == seen[0][0]
    assert doc["calibration"]["eval_rows"] == seen[0][1]
    assert seen[0][0] < doc["training_rows"]
    assert doc["effective_window"]["rows"] > 0
    assert doc["lead_lag"]["rows"] > 0
    assert "okx|BTC" in doc["venue_frame_coverage"]
    assert .02 <= training.probability(doc, [0.] * 18) <= .98


def test_original_frame_admission_never_fits_calibrates_or_promotes(monkeypatch):
    from parlay_bot.perpetuals import flow_challenger_v3

    frames, inputs = frames_and_inputs(n=3600, venues=("binance", "bitget", "okx"))
    candles = next(iter(frames.values()))[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only frame admission must not fit/evaluate weights")

    monkeypatch.setattr(training, "_fit_logistic", forbidden)
    monkeypatch.setattr(flow_challenger_v3, "fit_platt", forbidden)

    result = training.train_run(
        frames, asset="BTC", horizon=5, admission_only=True,
        train_end=candles.index[int(len(candles) * .66)],
        test_start=candles.index[int(len(candles) * .83)],
        trained_through=candles.index[-1],
        provenance={"provider_sha256_by_month": {"bitget:2026-11": "a" * 64},
                    "months_without_provider_hash": []},
        code_version="test-admission", market_data=inputs,
    )
    report = result["report"]
    assert result["publish_kind"] == "report" and result["artifact"] is None
    assert report["training_status"] == "admission_only"
    assert report["effective_window"]["rows"] > 0
    assert report["overlap_evidence"]["splits"]["test"]["rows"] > 0
    assert "admission_only_never_fits_or_publishes" in report["gate_failures"]


def test_groups_off_masks_ablated_inputs_and_keeps_rows():
    """Scheduled-grid ablations mask after the drop (volcal pattern):
    same rows as the full design, masked weights exactly 0, the
    ablation recorded on the report, and serving blind to the masked
    inputs. The joint gap goes with either market side."""
    frames, inputs = frames_and_inputs()
    full = run(frames, inputs)
    assert full["publish_kind"] == "model"
    for groups_off, masked in (
        (("dispersion",),
         ("venue_spread_ret1", "venue_up_share", "venue_spread_ret5")),
        (("pm", "gap"),
         ("pm_up", "pm_up_chg_3", "pm_up_chg_12", "kalshi_pm_gap")),
        (("kalshi", "gap"), ("kalshi_up", "kalshi_pm_gap")),
    ):
        result = run(frames, inputs, groups_off=groups_off)
        assert result["publish_kind"] == "model", result["report"].get("gate_failures")
        assert result["report"]["feature_groups_off"] == list(groups_off)
        assert (result["report"]["effective_window"]["rows"]
                == full["report"]["effective_window"]["rows"])
        doc = training.validate(result["artifact"])
        weights = doc["weights"]
        for name in masked:
            assert weights[doc["features"].index(name) + 1] == 0.0
        base = [0.3] * len(doc["features"])
        changed = list(base)
        for name in masked:
            changed[doc["features"].index(name)] = 5.0
        assert training.probability(doc, base) == training.probability(doc, changed)


def test_validate_rejects_unknown_ablation_group():
    doc = valid_doc()
    doc["feature_groups_off"] = ["nope"]
    with pytest.raises(ValueError, match="unsupported xmarket feature ablation"):
        training.validate(doc)


def test_noise_is_report_only():
    frames, inputs = frames_and_inputs(rho=0.)
    result = run(frames, inputs)
    assert result["publish_kind"] == "report"
    assert result["artifact"] is None
    assert result["report"]["gate_failures"]
    # Failed offline fits publish exploratory training-only evidence; never
    # generate importable weights or replace the sealed validation/test gates.
    research = result["report"]["train_only_feature_ablation"]
    assert research["research_only"] is True
    assert research["can_publish_model"] is False
    assert research["uses_outer_validation_or_test"] is False
    assert research["status"] in ("measured_train_only", "insufficient_train_only_fold")


def test_short_and_missing_histories_keep_measured_blocked_reports():
    frames, inputs = frames_and_inputs(n=600, venues=("bitget",))
    result = run(frames, inputs)
    assert result["artifact"] is None
    assert result["report"]["training_status"] == "blocked"
    assert result["report"]["input_count"] == 15
    assert result["report"]["effective_window"]["rows"] > 0
    assert "lead_lag" in result["report"]
    result = run(frames, {})
    assert result["report"]["gate_failures"] == ["missing verified prediction-market history"]


def test_stale_dispersion_sources_do_not_hide_real_market_overlap():
    frames, inputs = frames_and_inputs(
        n=3600, venues=("binance", "bitget", "okx"),
    )
    # Exact native sources and Binance extend through the test interval,
    # but the two supposed dispersion venues only have obsolete candles.
    for venue in ("bitget", "okx"):
        k, f = frames[f"{venue}|BTC"]
        frames[f"{venue}|BTC"] = (k.iloc[:200], f.iloc[:200])
    inputs["live_venues"] = ["bitget", "okx"]

    report = run(frames, inputs)["report"]
    assert report["input_count"] == 15
    assert report["dispersion_venues"] == []
    assert report["dispersion_selection"]["selection"] == "insufficient_joint_market_coverage"
    assert report["overlap_evidence"]["by_stream"]["binance|BTC"]["all_sources_rows"] > 0
    assert report["row_admission"]["binance|BTC"]["complete_rows"] > 0
    assert report["effective_window"]["rows"] > 0


def test_available_dispersion_pair_survives_a_third_stale_venue():
    frames, inputs = frames_and_inputs(
        n=3600, venues=("binance", "bitget", "okx", "gate"),
    )
    k, f = frames["gate|BTC"]
    frames["gate|BTC"] = (k.iloc[:100], f.iloc[:100])
    inputs["live_venues"] = ["bitget", "okx", "gate"]

    report = run(frames, inputs)["report"]
    assert report["input_count"] == 18
    assert report["dispersion_venues"] == ["bitget", "okx"]
    assert report["dispersion_selection"]["selection"] == "sufficient_observed_market_coverage"
    assert report["dispersion_selection"]["joint_split_rows"]["train"] >= 300
    assert report["row_admission"]["binance|BTC"]["complete_rows"] > 0


def test_recent_third_venue_cannot_erase_a_pair_with_all_chronological_splits(monkeypatch):
    frames, inputs = frames_and_inputs(n=18000, venues=("bitget", "okx", "gate"))
    index = frames["bitget|BTC"][0].index
    train_end, test_start = index[5000], index[13064]  # 28-day validation window
    k, f = frames["gate|BTC"]
    frames["gate|BTC"] = k.loc[train_end:], f.loc[train_end:]
    def forbidden(*args, **kwargs):
        raise AssertionError("admission must not fit or change a quality gate")
    monkeypatch.setattr(training, "_fit_logistic", forbidden)
    result = training.train_run(
        frames, asset="BTC", horizon=60, train_end=train_end, test_start=test_start,
        trained_through=index[-1], code_version="test", provenance={},
        market_data=inputs, admission_only=True,
    )
    report = result["report"]
    assert report["dispersion_venues"] == ["bitget", "okx"]
    assert report["input_count"] == 18
    assert report["dispersion_selection"]["all_split_capacity_present"] is True
    skipped = report["dispersion_selection"]["larger_material_subsets_missing_splits"]
    assert skipped[0]["venues"] == ["bitget", "okx", "gate"]
    assert skipped[0]["observed_pooled_split_capacity"]["train"] == 0
    assert report["overlap_evidence"]["eligible"] is True
    assert all(v["shortfall"] == 0 for v in report["overlap_evidence"]["splits"].values())
    assert result["artifact"] is None and report["gates"] == training.GATES


def test_young_material_archives_keep_the_largest_contract_and_original_split_block():
    frames, inputs = frames_and_inputs(n=600, venues=("bitget", "okx", "gate"))
    index = frames["bitget|BTC"][0].index
    result = training.train_run(
        frames, asset="BTC", horizon=5, train_end=index[0], test_start=index[100],
        trained_through=index[-1], code_version="test", provenance={}, market_data=inputs,
    )
    report = result["report"]
    assert report["dispersion_venues"] == ["bitget", "okx", "gate"]
    assert report["input_count"] == 18
    assert report["dispersion_selection"]["all_split_capacity_present"] is False
    assert report["overlap_evidence"]["minimum_rows"] == {"train": 300, "validation": 100, "test": 100}
    assert report["gate_failures"][0].startswith("split too small: train=0")
    assert result["artifact"] is None


@pytest.mark.parametrize("key,value", [
    ("schema", "wrong"), ("feature_digest", "0" * 64), ("weights", [0.]),
    ("weights", [float("nan")] * 19), ("weights_sha256", "0" * 64),
    ("standardizer", {"mean": [0.] * 18, "std": [0.] * 18}),
    ("gates_passed", False), ("data_checks", {}), ("horizon_minutes", 10),
])
def test_invalid_artifacts_never_enter_registry(key, value):
    doc = copy.deepcopy(valid_doc())
    doc[key] = value
    with pytest.raises((ValueError, TypeError)):
        training.validate(doc)


def test_no_imputation_at_inference_or_provider_hash_substitution():
    doc = valid_doc()
    with pytest.raises(ValueError):
        training.probability(doc, [float("nan")] * 18)
    doc["provenance"]["normalized_sha256_by_month"] = doc["provenance"]["provider_sha256_by_month"]
    with pytest.raises(ValueError, match="normalized"):
        training.validate(doc)


def test_provider_hash_commitments_keep_portable_artifacts_bounded():
    import hashlib
    import json

    samples = [{"source": s, "provider_sha256": hashlib.sha256(str(i).encode()).hexdigest()}
               for s in ("polymarket", "kalshi") for i in range(10_000)]
    provenance = training._supplement_provenance(samples)
    assert len(json.dumps(provenance)) < 2000
    assert provenance["supplemental_hash_sets"]["polymarket"]["provider_hashes_count"] == 10_000
    assert provenance["supplemental_hash_sets"]["kalshi"]["provider_hashes_count"] == 10_000
