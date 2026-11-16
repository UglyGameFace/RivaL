"""Read-only XMarket original-source evidence and sealed-holdout isolation."""
from __future__ import annotations

import asyncio
import json

import pytest

from parlay_bot.perpetuals import flow_challenger_v3
from parlay_bot.perpetuals.xmarket_inputs import BUNDLE_SCHEMA
from parlay_bot.research import xmarket_history, xmarket_training
from tests.xmarket_fixtures import frames_and_inputs


def test_research_only_never_opens_outer_validation_test_scores_or_model(monkeypatch):
    frames, inputs = frames_and_inputs(n=15_000)
    candles = next(iter(frames.values()))[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("outer validation/test scoring must not run")

    monkeypatch.setattr(flow_challenger_v3, "fit_platt", forbidden)
    monkeypatch.setattr(xmarket_training, "_metrics", forbidden)
    monkeypatch.setattr(xmarket_training, "venue_holdout", forbidden)
    result = xmarket_training.train_run(
        frames, asset="BTC", horizon=5, research_only=True,
        train_end=candles.index[10_000],
        test_start=candles.index[12_500],
        trained_through=candles.index[-1], code_version="research-only-test",
        provenance={"provider_sha256_by_month": {"bitget:2026-01": "a" * 64},
                    "months_without_provider_hash": []},
        market_data=inputs,
    )
    assert result["publish_kind"] == "report"
    assert result["artifact"] is None
    report = result["report"]
    assert report["training_status"] == "research_only"
    assert report["outer_validation_test_scored"] is False
    measured = report["train_only_feature_ablation"]
    assert measured["status"] == "measured_train_only"
    assert measured["uses_outer_validation_or_test"] is False
    assert measured["can_publish_model"] is False


def test_research_only_is_mutually_exclusive_with_admission_mode():
    with pytest.raises(ValueError, match="exclusive"):
        xmarket_training.train_run({}, asset="BTC", horizon=5,
            train_end="2026-09-19T00:00:00+00:00",
            test_start="2026-10-17T00:00:00+00:00",
            trained_through="2026-11-13T23:55:00+00:00",
            provenance={}, code_version="test",
            admission_only=True, research_only=True)


def test_read_only_bundle_restore_never_invokes_backfill_or_writes(monkeypatch):
    from datetime import UTC, datetime

    from tests.xmarket_fixtures import point

    class FakeDrive:
        def __init__(self):
            self.calls = []

        async def read_verified_research_reports(self, **kwargs):
            self.calls.append(kwargs)
            time = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
            bundle = {"schema": BUNDLE_SCHEMA, "asset": "BTC",
                      "created_at": time.isoformat(),
                      "samples": [point("polymarket", time),
                                  point("kalshi", time)]}
            return [("opaque-drive-file-id", json.dumps(bundle).encode())], None

        def __getattr__(self, name):
            raise AssertionError(f"unexpected Drive mutation or provider call: {name}")

    async def forbidden(*args, **kwargs):
        raise AssertionError("original source must not be re-fetched or published")

    monkeypatch.setattr(xmarket_history, "weekly_inputs_once", forbidden)
    drive = FakeDrive()
    result = asyncio.run(xmarket_history.read_verified_inputs_only(drive, asset="BTC"))
    assert len(drive.calls) == 1
    assert drive.calls[0]["name_prefix"].endswith("BTC-")
    assert len(result["samples"]) == 2
    assert all(p["asset"] == "BTC" for p in result["samples"])
    assert len(result["receipts"]) == 1
    assert result["verification"]["polymarket"]["status"] == "unverified"


def test_read_only_bundle_restore_refuses_cross_asset_readback():
    class CrossAssetDrive:
        async def read_verified_research_reports(self, **kwargs):
            bundle = {"schema": BUNDLE_SCHEMA, "asset": "ETH", "samples": []}
            return [("opaque-id", json.dumps(bundle).encode())], None

    with pytest.raises(ValueError, match="cross-asset"):
        asyncio.run(xmarket_history.read_verified_inputs_only(CrossAssetDrive(), asset="BTC"))
