"""Phase 1: archive_v1 trainer, features, calibration, provenance, driver.

Every test traces to the spec: §2.5 feature contract, §2.6 walk-forward
+ labels + no-refit, §2.7 calibration, §2.10.3 context-absent gate,
§2.3 artifact/provenance, §3 option B driver, and the GHA workflow.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.research.archive_features import (
    FEATURE_CONTRACT,
    FEATURE_DIGEST,
    FEATURES,
    GROUPS,
    frame_features,
    live_vector,
)
from parlay_bot.research.archive_training import (
    GATES,
    build_labels,
    census_summary,
    chronological_splits,
    probability,
    train_run,
    usable,
    validate,
)
from parlay_bot.research.archive_training import (
    main as train_main,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
PROV = {
    "provider_sha256_by_month": {"binance:2025-01": "a" * 64, "bitget:2025-01": "b" * 64},
    "months_without_provider_hash": [],
    "manifest_sha256_by_month": {"binance:2025-01": "c" * 64},
}


def _synthetic_frame(
    n_bars: int,
    seed: int,
    *,
    momentum: float = 0.6,
    tape_signal: float = 0.0,
    start: str = "2025-01-01",
):
    """5m candles whose next-bar return carries momentum and/or tape signal.

    Returns are generated sequentially, so every feature at bar t is known
    at t and no future information leaks into the frame.
    """
    rng = np.random.default_rng(seed)
    times = pd.date_range(start, periods=n_bars, freq="5min", tz=UTC)
    close_times = times + pd.Timedelta(minutes=5)
    r = np.zeros(n_bars)
    imb = np.zeros(n_bars)
    # Exogenous bounded tape signal (used only in the tape-only fixture):
    # an AR(1) process clipped to [-0.3, 0.3], independent of returns, so
    # the tape-driven fixture cannot feed back into exploding prices.
    s = np.zeros(n_bars)
    for t in range(1, n_bars):
        s[t] = float(np.clip(0.95 * s[t - 1] + rng.normal(0, 0.08), -0.3, 0.3))
    for t in range(1, n_bars):
        if tape_signal and momentum == 0.0:
            imb[t - 1] = 0.5 + s[t - 1]
            r[t] = tape_signal * s[t - 1] + rng.normal(0, 0.0015)
        else:
            imb[t - 1] = 0.5 + 0.3 * math.tanh(50 * r[t - 1])
            r[t] = (
                momentum * r[t - 1] + tape_signal * (imb[t - 1] - 0.5) * 2 + rng.normal(0, 0.0015)
            )
    imb[-1] = 0.5 + (s[-1] if tape_signal and momentum == 0.0 else 0.3 * math.tanh(50 * r[-1]))
    price = 100 * np.exp(np.cumsum(r))
    volume = 1000 * (1 + np.abs(r) * 5000)
    k = pd.DataFrame(
        {
            "open": np.concatenate([[price[0]], price[:-1]]),
            "high": price * 1.0002,
            "low": price * 0.9998,
            "close": price,
            "volume": volume,
            "trades": 10,
            "taker_buy_volume": volume * imb,
            "count": 100,
            "prem": 0.0002 * np.tanh(pd.Series(r).rolling(12).mean().fillna(0).to_numpy() * 100),
            "fund": 0.0001,
            "first_observed_at": close_times + pd.Timedelta(milliseconds=1),
        },
        index=close_times,
    )
    return k


def _frames(n_bars: int = 17280, **kwargs):
    """Two streams (binance BTC, bitget BTC) + BTC cross frame."""
    out = {}
    for venue, seed in (("binance", 1), ("bitget", 2)):
        k = _synthetic_frame(n_bars, seed, **kwargs)
        out[f"{venue}|BTC"] = (k, frame_features(k, None, "BTC"))
    return out


def test_restored_altcoin_uses_btc_as_reference_only(tmp_path, monkeypatch):
    from parlay_bot.research import archive_training, learning

    for coin in ("BTC", "ETH"):
        identity = {"venue": "okx", "contract": f"{coin}-USDT-SWAP", "quote": "USDT"}
        (tmp_path / f"{coin}.manifest.json").write_text(json.dumps(identity), encoding="utf-8")

    def assemble(paths):
        identity = json.loads(paths[0].read_text(encoding="utf-8"))
        btc = identity["contract"].startswith("BTC-")
        candles = _synthetic_frame(450, 1 if btc else 2).rename(columns={"volume": "volume_base"})
        candles["closed_at"] = candles.index
        return identity, candles.to_dict("records"), {
            "provider_sha256_by_month": {"2025-01": ("a" if btc else "b") * 64},
        }

    monkeypatch.setattr(learning, "assemble", assemble)
    frames, provenance = archive_training.load_frames_manifests(tmp_path, target_asset="ETH")
    assert list(frames) == ["okx|ETH-USDT-SWAP|USDT"]
    assert math.isfinite(frames["okx|ETH-USDT-SWAP|USDT"][1]["btc_ret_12"].iloc[-1])
    assert provenance["provider_sha256_by_month"] == {"okx:2025-01": "b" * 64}
    assert provenance["reference_provider_sha256_by_month"] == {"okx:2025-01": "a" * 64}


def _splits():
    # 60 days of bars: train ~40d, validation ~10d, test ~10d.
    train_end = datetime(2025, 2, 10, tzinfo=UTC)
    test_start = datetime(2025, 2, 20, tzinfo=UTC)
    trained_through = datetime(2025, 3, 1, 23, 55, tzinfo=UTC)
    return train_end, test_start, trained_through


def test_feature_contract_is_32_features_in_frozen_groups():
    assert len(FEATURES) == 32
    assert len(set(FEATURES)) == 32
    covered = sorted(i for idxs in GROUPS.values() for i in idxs)
    assert covered == list(range(32))
    assert FEATURE_CONTRACT == "archive-candle-premium-calendar-v1"
    assert (
        FEATURE_DIGEST
        == hashlib.sha256(
            json.dumps([FEATURE_CONTRACT, *FEATURES], separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_frame_and_live_vector_agree():
    k = _synthetic_frame(600, 7)
    frame = frame_features(k, None, "BTC")
    candles = [
        MarketCandle(
            venue="binance",
            contract="BTCUSDT",
            quote="USDT",
            interval_minutes=5,
            opened_at=row.name - timedelta(minutes=5),
            closed_at=row.name,
            observed_at=row.name + timedelta(milliseconds=1),
            open_price=float(row["open"]),
            high_price=float(row["high"]),
            low_price=float(row["low"]),
            close_price=float(row["close"]),
            volume_base=float(row["volume"]),
            trades=10,
        )
        for _, row in k.iterrows()
    ]
    bundle = {
        "klines": [
            (idx, float(row["volume"]), float(row["taker_buy_volume"]), float(row["count"]))
            for idx, row in k.iterrows()
        ],
        "premium": {idx: float(row["prem"]) for idx, row in k.iterrows()},
        "funding": [(idx, 0.0001) for idx in k.index[::96]],
    }
    values, flags = live_vector(candles, asset="BTC", context=bundle)
    last = frame.iloc[-1]
    for name, value in zip(FEATURES, values):
        # BTC's own cross-asset features are NaN by pooled design
        # (an asset is not its own BTC lead); both sides must agree.
        if math.isnan(value):
            assert math.isnan(float(last[name])), name
            continue
        assert abs(float(last[name]) - value) < 1e-4, name
    assert flags == (1.0,) * 7
    # No context at all: premium/funding/tape are NaN with 0 flags, never 0.
    values2, flags2 = live_vector(candles, asset="BTC", context=None)
    for name in ("prem", "fund", "taker_imb"):
        assert math.isnan(values2[FEATURES.index(name)]), name
    assert flags2[4] == flags2[5] == flags2[6] == 0.0
    assert flags2[0] == 1.0


def test_premium_gap_is_missing_data_never_zero_filled():
    k = _synthetic_frame(600, 7)
    k.loc[k.index[:300], "prem"] = np.nan  # the Nov 2024-Feb 2025 shape
    frame = frame_features(k, None, "BTC")
    assert frame["prem"].iloc[:300].isna().all()
    assert not (frame["prem"].iloc[:300] == 0).any()
    assert frame["prem"].iloc[300:].notna().any()


def test_labels_follow_settlement_math():
    k = _synthetic_frame(100, 3, momentum=0.0)
    y = build_labels(k, 5)
    # Strict UP: flat target is NOT UP, never dropped.
    flat_idx = k.index[10]
    k2 = k.copy()
    k2.loc[k2.index[11], "close"] = k2.loc[flat_idx, "close"]
    y2 = build_labels(k2, 5)
    assert y2.iloc[10] == 0
    # Flat target with zero trades is withdrawn, not NOT UP.
    k3 = k2.copy()
    k3.loc[k3.index[11], "trades"] = 0
    y3 = build_labels(k3, 5)
    assert math.isnan(y3.iloc[10])
    # Anti-backfill: outcome first observed before the issuance close.
    k4 = k.copy()
    k4.loc[k4.index[11], "first_observed_at"] = k4.index[10] - timedelta(minutes=1)
    y4 = build_labels(k4, 5)
    assert math.isnan(y4.iloc[10])
    assert y4.iloc[11] == y.iloc[11] or math.isnan(y4.iloc[11])


def test_walk_forward_splits_purge_one_horizon():
    index = pd.date_range("2025-01-01", periods=2000, freq="5min", tz=UTC)
    train_end = datetime(2025, 1, 5, tzinfo=UTC)
    test_start = datetime(2025, 1, 6, tzinfo=UTC)
    train, val, test = chronological_splits(
        index, train_end=train_end, test_start=test_start, horizon=60
    )
    assert index[train].max() < train_end - timedelta(minutes=60)
    assert index[val].min() >= train_end
    assert index[val].max() < test_start - timedelta(minutes=60)
    assert index[test].min() >= test_start
    assert not (train & val).any() and not (val & test).any()


def test_train_passes_gates_and_publishes_evaluated_fit():
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames(),
        asset="BTC",
        horizon=5,
        train_end=train_end,
        test_start=test_start,
        trained_through=trained_through,
        provenance=PROV,
        code_version="test-sha",
    )
    assert result["publish_kind"] == "model", result["report"]
    doc = validate(result["artifact"])
    assert usable(doc, now=datetime(2025, 3, 2, tzinfo=UTC))
    ev = doc["evaluation"]
    assert ev["validation"]["brier_gain"] > GATES["min_validation_gain"]
    assert ev["test"]["brier_gain"] > GATES["min_test_gain"]
    assert ev["test"]["daily_positive_fraction"] >= GATES["min_test_daily_positive"]
    assert ev["test"]["ece"] <= GATES["max_test_ece"]
    assert ev["context_absent_test"]["brier_gain"] > 0.005
    assert doc["serving_capable_without_binance_context"] is True
    assert doc["calibration"]["status"] == "evaluated"
    # Platt was fitted on the validation window only, gated on test.
    assert doc["calibration"]["fit_rows"] == ev["validation"]["rows"]
    assert doc["calibration"]["eval_rows"] == ev["test"]["rows"]
    assert len(doc["weights"]) == 33
    assert doc["evaluation_status"] == "held-out evaluated"
    assert doc["provenance"]["provider_sha256_by_month"]["binance:2025-01"] == "a" * 64
    assert doc["serve_clamp"] == [0.02, 0.98]
    assert ev["venue_holdout_28d"], "28-day venue holdout must be reported"
    p = probability(doc, [0.0] * 32)
    assert 0.02 <= p <= 0.98
    p_missing = probability(doc, [float("nan")] * 32)
    assert 0.02 <= p_missing <= 0.98


def test_no_signal_run_publishes_report_only():
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames(momentum=0.0, tape_signal=0.0),
        asset="BTC",
        horizon=5,
        train_end=train_end,
        test_start=test_start,
        trained_through=trained_through,
        provenance=PROV,
        code_version="test-sha",
    )
    assert result["publish_kind"] == "report"
    assert result["artifact"] is None
    assert result["report"]["gate_failures"]


def test_refit_artifact_is_refused():
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames(),
        asset="BTC",
        horizon=5,
        train_end=train_end,
        test_start=test_start,
        trained_through=trained_through,
        provenance=PROV,
        code_version="test-sha",
    )
    doc = dict(result["artifact"])
    doc["evaluation_status"] = "backtest-only, shipped model unevaluated"
    assert not usable(doc, now=datetime(2025, 3, 2, tzinfo=UTC))
    doc2 = dict(result["artifact"])
    doc2["gates_passed"] = False
    assert not usable(doc2, now=datetime(2025, 3, 2, tzinfo=UTC))


def test_context_absent_failure_blocks_serving_capable_artifact():
    # Only the tape group predicts: momentum 0, tape drives next return.
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames(momentum=0.0, tape_signal=0.02),
        asset="BTC",
        horizon=5,
        train_end=train_end,
        test_start=test_start,
        trained_through=trained_through,
        provenance=PROV,
        code_version="test-sha",
    )
    assert result["publish_kind"] == "report", result["report"]["evaluation"]
    assert "context_absent_gain<=0.005" in result["report"]["gate_failures"]
    assert result["report"]["evaluation"]["test"]["brier_gain"] > 0.005
    assert result["report"]["evaluation"]["context_absent_test"]["brier_gain"] <= 0.005


def test_provenance_refuses_normalized_hash_as_provider_hash():
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames(),
        asset="BTC",
        horizon=5,
        train_end=train_end,
        test_start=test_start,
        trained_through=trained_through,
        provenance=PROV,
        code_version="test-sha",
    )
    doc = dict(result["artifact"])
    doc["provenance"] = dict(PROV)
    doc["provenance"]["normalized_sha256_by_month"] = {"binance:2025-01": "a" * 64}
    with pytest.raises(ValueError, match="normalized hash"):
        validate(doc)
    doc2 = dict(result["artifact"])
    doc2["provenance"] = {"provider_sha256_by_month": {}, "months_without_provider_hash": []}
    with pytest.raises(ValueError, match="provider hashes"):
        validate(doc2)
    doc3 = dict(result["artifact"])
    doc3["calibration"] = {"status": "failed_gate", "a": 1.0, "b": 0.0}
    with pytest.raises(ValueError, match="calibration"):
        validate(doc3)


def test_census_summary_measures_before_training(tmp_path):
    census = {
        "duplicate_manifest_names": 0,
        "month_scopes": [
            {
                "venue": "bitget",
                "asset": "BTC",
                "family": "market",
                "listed_manifest_months": 3,
                "not_listed_months": ["2025-02"],
                "conflicting_months": [],
                "duplicate_name_months": [],
            },
        ],
    }
    path = tmp_path / "census.json"
    path.write_text(json.dumps(census), encoding="utf-8")
    out = census_summary(path)
    assert out["scopes"][0]["not_listed_months"] == ["2025-02"]
    assert "byte-verified" in out["note"]


def test_dry_run_cli_writes_model_and_report(tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for venue, seed in (("binance", 1), ("bitget", 2)):
        k = _synthetic_frame(17280, seed)
        out = k.reset_index().rename(columns={"index": "time"})
        out.to_csv(frames_dir / f"{venue}__BTC.csv", index=False)
    (tmp_path / "prov.json").write_text(json.dumps(PROV), encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = train_main(
        [
            "--asset",
            "BTC",
            "--horizon",
            "5",
            "--frames-dir",
            str(frames_dir),
            "--provenance",
            str(tmp_path / "prov.json"),
            "--train-end",
            "2025-02-10T00:00:00+00:00",
            "--test-start",
            "2025-02-20T00:00:00+00:00",
            "--trained-through",
            "2025-03-01T23:55:00+00:00",
            "--out",
            str(out_dir),
            "--code-version",
            "test-sha",
        ]
    )
    assert rc == 0
    assert (out_dir / "archive-v1-BTC-5m-model.json").exists()
    assert (out_dir / "archive-v1-BTC-5m-report.json").exists()
    doc = json.loads((out_dir / "archive-v1-BTC-5m-model.json").read_text())
    assert validate(doc)["family"] == "archive_v1"


def test_workflow_yaml_is_weekly_dispatch_only():
    # No PyYAML in the research venv: check the workflow text directly.
    text = (
        Path(__file__).resolve().parents[1] / ".github/workflows/archive-model-training.yml"
    ).read_text()
    assert 'cron: "30 7 * * 0"' in text  # weekly, Sunday
    assert "workflow_dispatch:" in text
    assert "on:\n  push" not in text and "\n  push:" not in text
    assert "runs-on: ubuntu-latest" in text
    assert "timeout-minutes: 240" in text
    assert "cancel-in-progress: false" in text
    assert "asset: [BTC, ETH, SOL, XRP]" in text
    assert "drive_source_census" in text
    assert "restore_archive_months" in text
    assert "train_archive_model" in text
    assert 'python-version: "3.11"' in text


# --- Minimal research-loop driver (D10 option B) ---------------------------


def _receipt():
    from parlay_bot.perpetuals.research_hypotheses import MODEL

    identity = {
        "asset": "BTC",
        "slot_utc": NOW.isoformat(),
        "model": MODEL,
        "evidence_sha256": "c" * 64,
        "hypothesis": "volume_only",
        "venues": ["hyperliquid"],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "status": "verified",
        "source": "live_postgresql_llm_hypotheses",
        "proposals": [{**identity, "record_sha256": digest}],
    }


def _passing_trial():
    return {
        "asset": "BTC",
        "venue": "hyperliquid",
        "contract": "BTC",
        "horizon_minutes": 5,
        "hypothesis": "volume_only",
        "feature_indices": [4, 9],
        "proposal_sha256": "a" * 64,
        "validation_brier_delta": 0.012,
        "holdout_brier_delta": 0.011,
        "holdout_examined": True,
        "retrospective_candidate_status": "screened_for_future_shadow",
    }


def test_driver_runs_verify_ablate_finding(tmp_path):
    from parlay_bot.perpetuals.research_loop import transition
    from parlay_bot.research.archive_driver import (
        drive_archive_feature_hypotheses,
    )

    store = SQLiteHotStore(tmp_path / "loop.sqlite")
    result = drive_archive_feature_hypotheses(
        store,
        asset="BTC",
        receipt=_receipt(),
        venue="hyperliquid",
        trials=[_passing_trial()],
        now=NOW,
    )
    assert result["experiment"]["status"] == "screened"
    assert result["findings"], "every closed experiment leaves a finding"
    # Failing ablation discards with a finding, never silently.
    store2 = SQLiteHotStore(tmp_path / "loop2.sqlite")
    trial = _passing_trial()
    trial["holdout_brier_delta"] = 0.002
    trial["retrospective_candidate_status"] = "ordinary_holdout_not_beaten"
    result2 = drive_archive_feature_hypotheses(
        store2, asset="BTC", receipt=_receipt(), venue="hyperliquid", trials=[trial], now=NOW
    )
    assert result2["experiment"]["status"] == "discarded"
    assert result2["findings"]
    # Actor llm is refused by the loop itself.
    store3 = SQLiteHotStore(tmp_path / "loop3.sqlite")
    from parlay_bot.perpetuals.research_loop import create_experiment

    exp = create_experiment(
        store3, proposer="human", hypothesis="volume_only", design={"asset": "BTC"}, now=NOW
    )
    with pytest.raises(ValueError, match="llm"):
        transition(store3, exp["experiment_id"], "ablating", actor="llm")
