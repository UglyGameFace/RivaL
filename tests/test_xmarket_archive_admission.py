"""Native archive stream identities and causal feature-close admission."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from parlay_bot.research import xmarket_training
from tests.xmarket_fixtures import NOW, frames_and_inputs, point


def native_frames(frames):
    contracts = {"bitget": "BTCUSDT", "okx": "BTC-USDT-SWAP"}
    result = {}
    for key, (k, f) in frames.items():
        venue = key.split("|")[0]
        k, f = k.copy(deep=False), f.copy(deep=False)
        k.index = f.index = k.index - pd.Timedelta(milliseconds=1)
        result[f"{venue}|{contracts[venue]}|USDT"] = k, f
    return result


def training_args(frames):
    index = next(iter(frames.values()))[0].index
    return {"asset": "BTC", "horizon": 5, "train_end": index[int(len(index) * .66)],
                "test_start": index[int(len(index) * .83)], "trained_through": index[-1],
                "code_version": "fixture", "provenance": {
                    "provider_sha256_by_month": {"bitget:2026-01": "a" * 64},
                    "months_without_provider_hash": []}}


def test_real_archive_keys_and_native_close_clock_fit_the_same_causal_model():
    frames, inputs = frames_and_inputs()
    native = native_frames(frames)
    params = training_args(frames)
    reference = xmarket_training.train_run(frames, market_data=inputs, **params)
    result = xmarket_training.train_run(native, market_data=inputs, **params)
    assert result["publish_kind"] == reference["publish_kind"] == "model"
    model = xmarket_training.validate(result["artifact"])
    assert set(model["venue_frame_coverage"]) == set(native)
    assert set(model["coverage"]["streams"]) == set(native)
    assert model["dispersion_venues"] == ["bitget", "okx"]
    assert model["effective_window"] == reference["report"]["effective_window"]
    np.testing.assert_allclose(model["weights"], reference["artifact"]["weights"])
    # Normalization never rewrites source frames shared with peer trainers.
    assert all(k.index[0].microsecond == 999000 for k, _ in native.values())


@pytest.mark.parametrize("defect", ["off_grid", "duplicate", "naive", "mismatched_features"])
def test_invalid_archive_clocks_cannot_be_snapped_into_valid_training_bars(defect):
    frames, _ = frames_and_inputs(n=10, venues=("bitget",))
    k, f = frames["bitget|BTC"]
    if defect == "off_grid":
        k.index = f.index = k.index - pd.Timedelta(seconds=1)
    elif defect == "duplicate":
        k.index = f.index = pd.DatetimeIndex([k.index[0]] * len(k))
    elif defect == "naive":
        k.index = f.index = k.index.tz_localize(None)
    else:
        f.index = f.index + pd.Timedelta(minutes=5)
    with pytest.raises(ValueError, match="clock|close grid"):
        xmarket_training._training_frames(frames, asset="BTC")


def test_archive_sources_remain_asset_and_quote_specific():
    frames, _ = frames_and_inputs(n=10, venues=("bitget",))
    pair = frames["bitget|BTC"]
    selected, pinned = xmarket_training._training_frames({
        "bitget|BTCUSDT|USDT": pair, "bitget|ETHUSDT|USDT": pair}, asset="BTC")
    assert list(selected) == ["bitget|BTCUSDT|USDT"]
    assert list(pinned) == ["bitget|BTC"]
    with pytest.raises(ValueError, match="ambiguous"):
        xmarket_training._training_frames({"bitget|BTCUSDT|USDT": pair,
                                           "bitget|BTCUSDC|USDC": pair}, asset="BTC")


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "XRP"])
@pytest.mark.parametrize("horizon", [5, 15, 60])
def test_all_asset_horizons_admit_native_streams_without_bypassing_split_gates(asset, horizon):
    frames, inputs = frames_and_inputs(n=600)
    native = {key.replace("BTC", asset): pair for key, pair in native_frames(frames).items()}
    inputs["samples"] = [point(r["source"], pd.Timestamp(r["at"]).to_pydatetime(),
                               r["price"], asset=asset) for r in inputs["samples"]]
    params = {**training_args(frames), "asset": asset, "horizon": horizon}
    result = xmarket_training.train_run(native, market_data=inputs, **params)
    report = result["report"]
    assert set(report["venue_frame_coverage"]) == set(native)
    assert report["effective_window"]["rows"] > 0
    assert report["dispersion_venues"] == ["bitget", "okx"]
    assert report["training_status"] == "blocked"
    assert report["gate_failures"][0].startswith("split too small:")
    assert result["artifact"] is None


def test_native_clocks_keep_exact_market_joins_and_training_cutoff():
    frames, inputs = frames_and_inputs(n=600)
    native = native_frames(frames)
    params = training_args(frames)
    before = xmarket_training.train_run(native, market_data=inputs, **params)
    assert before["report"]["effective_window"]["rows"] > 0
    # A point just after the feature boundary cannot fill the previous bar.
    inputs["samples"] = [r for r in inputs["samples"] if r["at"] != r["closes_at"]]
    for row in inputs["samples"]:
        row["at"] = row["observed_at"]
    after = xmarket_training.train_run(native, market_data=inputs, **params)
    assert after["artifact"] is None
    assert after["report"]["effective_window"]["rows"] == 0
    # Native normalization cannot include a bar beyond trained_through.
    params["trained_through"] = next(iter(frames.values()))[0].index[450]
    _, inputs = frames_and_inputs(n=600)
    bounded = xmarket_training.train_run(native, market_data=inputs, **params)
    assert pd.Timestamp(bounded["report"]["effective_window"]["last"]) < params["trained_through"]


@pytest.mark.parametrize("key", ["bitget", "bitget||USDT", "bitget|BTCUSDT|USDT|extra"])
def test_invalid_archive_stream_identity_is_rejected(key):
    frames, _ = frames_and_inputs(n=10, venues=("bitget",))
    with pytest.raises(ValueError, match="stream identity"):
        xmarket_training._training_frames({key: frames["bitget|BTC"]}, asset="BTC")


def test_missing_asset_keeps_no_frames_blocked_report():
    frames, inputs = frames_and_inputs(n=600)
    frames = {key.replace("BTC", "ETH"): pair for key, pair in native_frames(frames).items()}
    result = xmarket_training.train_run(frames, market_data=inputs, **training_args(frames))
    assert result["report"]["gate_failures"] == ["no frames to train on"]
    assert result["artifact"] is None


def test_disjoint_source_dates_report_measured_admission_shortfall():
    frames, inputs = frames_and_inputs(n=600)
    inputs["samples"] = [point(r["source"], (pd.Timestamp(r["at"]) + pd.Timedelta(days=10))
                               .to_pydatetime(), r["price"]) for r in inputs["samples"]]
    native = native_frames(frames)
    result = xmarket_training.train_run(native, market_data=inputs, **training_args(frames))
    report = result["report"]
    assert report["gate_failures"] == ["not enough labelled overlap rows: 0"]
    assert report["split_rows"] == {"train": 0, "validation": 0, "test": 0}
    for stream, counts in report["row_admission"].items():
        assert counts["candle_feature_rows"] > 0 and counts["labelled_rows"] > 0
        assert counts["market_feature_rows"] == counts["complete_rows"] == 0
        assert (pd.Timestamp(report["source_feature_coverage"]["polymarket"]["first"])
                > pd.Timestamp(report["venue_frame_coverage"][stream]["last"]))
    assert result["artifact"] is None



@pytest.mark.asyncio
async def test_weekly_driver_consumes_restored_manifest_sources_without_fake_admission(tmp_path, monkeypatch):
    from parlay_bot.research import archive_training, learning, weekly_archive, xmarket_history

    frames, inputs = frames_and_inputs(n=600)
    identities = {"bitget": "BTCUSDT", "okx": "BTC-USDT-SWAP"}
    for venue, contract in identities.items():
        (tmp_path / f"{venue}.manifest.json").write_text(json.dumps({
            "venue": venue, "contract": contract, "quote": "USDT"}))

    def assemble(paths):
        identity = json.loads(paths[0].read_text())
        k = frames[f"{identity['venue']}|BTC"][0].rename(columns={"volume": "volume_base"})
        k = k.assign(closed_at=k.index - pd.Timedelta(milliseconds=1))
        return identity, k.to_dict("records"), {"provider_sha256_by_month": {"2026-01": "a" * 64}}

    monkeypatch.setattr(learning, "assemble", assemble)
    restored, provenance = archive_training.load_frames_manifests(tmp_path, target_asset="BTC")
    bundle = {"schema": "rival-xmarket-inputs-v1", "asset": "BTC", "samples": inputs["samples"],
              "live_venues": inputs["live_venues"], "created_at": NOW.isoformat()}
    class Drive:
        async def read_verified_research_reports(self, **kwargs):
            return [("fixture-native-inputs", json.dumps(bundle).encode())], None
        async def ensure_research_output(self, **kwargs):
            return SimpleNamespace(file_id="fixture-published-report")
        async def aclose(self):
            pass
    monkeypatch.setattr(weekly_archive, "_drive", lambda _: Drive())
    monkeypatch.setattr(xmarket_history, "backfill", lambda *a, **kw:
                        ([{**r, "origin": "backfill"} for r in inputs["samples"]], [], NOW, []))
    original_fit = weekly_archive._fit
    def fit(module, frames, training, **kwargs):
        if module is xmarket_training:
            return original_fit(module, frames, training, **kwargs)
        return {"gates_passed": False}, None, "blocked"
    monkeypatch.setattr(weekly_archive, "_fit", fit)
    params = {**training_args(frames), "provenance": provenance}
    result = await weekly_archive.train_weekly_archive(restored, training=params, census={},
                                                       out=tmp_path / "models", publish=True)
    report = json.loads((tmp_path / "models/xmarket-v1-BTC-5m-report.json").read_text())
    assert result == 0 and report["training_status"] == "blocked"
    assert set(report["venue_frame_coverage"]) == set(restored)
    assert report["effective_window"]["rows"] > 0
    assert report["split_rows"]["train"] < 300
    assert all(c["status"] == "verified" for c in report["data_checks"].values())
    assert not (tmp_path / "models/xmarket-v1-BTC-5m-model.json").exists()
