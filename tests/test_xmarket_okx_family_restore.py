"""Exact OKX provenance selection for offline XMarket training only.

The default restoration remains strict across competing source identities.
An explicit selected family never lets a pre-existing ledger snapshot enter
the same training frame. Source manifests still pass original SHA/grid proof.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parlay_bot.research import archive_training

PIN = "okx-v5-public-history-5m-v1"
OTHER = "okx-v5-official-1m-zip-derived-5m-v1"


@pytest.mark.parametrize("pin", [None, PIN, OTHER])
def test_month_restore_segregates_pinned_okx_without_changing_other_sources(
    tmp_path, monkeypatch, pin,
):
    from parlay_bot.research import (
        bitget_source_bridge,
        recorded_candles,
        source_native,
    )

    calls = []

    async def restore_bitget(_drive, *, months, root, asset):
        calls.append(("bitget", asset, str(root), None))
        return []

    async def restore_native(_drive, *, venue, months, root, contract,
                             source_integrity=None):
        calls.append((venue, contract, str(root), source_integrity))
        return []

    async def restore_recorded(_drive, **_kwargs):
        return {"restored": []}

    monkeypatch.setattr(bitget_source_bridge, "restore_verified_bitget_market_months",
                        restore_bitget)
    monkeypatch.setattr(source_native, "restore_verified_source_months",
                        restore_native)
    monkeypatch.setattr(recorded_candles, "restore_recorded_partitions",
                        restore_recorded)
    result = asyncio.run(archive_training.restore_verified_months(
        asset="BTC", months=["2026-05"], root=tmp_path,
        drive=object(), now=datetime(2026, 11, 16, tzinfo=UTC),
        okx_source_integrity=pin,
    ))
    expected = (tmp_path / "source-families" / pin) if pin else tmp_path
    assert ("okx", "BTC-USDT-SWAP", str(expected), pin) in calls
    assert ("gate", "BTC_USDT", str(tmp_path), None) in calls
    assert result["okx_selected_source_integrity"] == pin
    assert not result["excluded"]
    assert json.loads((tmp_path / "restore-report.json").read_text()) == result


def test_unknown_okx_source_family_fails_before_drive_access(tmp_path):
    with pytest.raises(ValueError, match="unsupported exact OKX"):
        asyncio.run(archive_training.restore_verified_months(
            asset="BTC", months=["2026-05"], root=tmp_path,
            drive=object(), okx_source_integrity="unverified-family",
        ))
    with pytest.raises(ValueError, match="unsupported exact OKX"):
        archive_training.load_frames_manifests(
            tmp_path, okx_source_integrity="unverified-family",
        )


def _manifest(directory: Path, name: str, *, venue: str, integrity: str | None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({
        "venue": venue,
        "contract": "BTC-USDT-SWAP" if venue == "okx" else "BTC_USDT",
        "quote": "USDT",
        "source_integrity": integrity,
    }))
    return path


def test_pinned_training_skips_prior_okx_ledger_original_but_keeps_other_venues(
    tmp_path, monkeypatch,
):
    from parlay_bot.research import learning
    from tests.test_archive_model import _synthetic_frame

    old = _manifest(
        tmp_path / "verified_venue_perp_klines",
        "old-okx.manifest.json", venue="okx", integrity="postgres-first-observed-at-v1",
    )
    selected = _manifest(
        tmp_path / "source-families" / PIN / "verified_venue_perp_klines",
        "provider-okx.manifest.json", venue="okx", integrity=PIN,
    )
    gate = _manifest(
        tmp_path / "verified_venue_perp_klines",
        "gate.manifest.json", venue="gate", integrity=None,
    )
    seen = []

    def assemble(paths):
        assert len(paths) == 1
        path = paths[0]
        seen.append(path)
        info = json.loads(path.read_text())
        candles = _synthetic_frame(440, 1)
        candles = candles.rename(columns={"volume": "volume_base"})
        candles["closed_at"] = candles.index
        return info, candles.to_dict("records"), {
            "provider_sha256_by_month": {"2026-05": "a" * 64},
        }

    monkeypatch.setattr(learning, "assemble", assemble)
    frames, provenance = archive_training.load_frames_manifests(
        tmp_path, target_asset="BTC", okx_source_integrity=PIN,
    )
    assert selected in seen and gate in seen and old not in seen
    assert set(frames) == {"okx|BTC-USDT-SWAP|USDT", "gate|BTC_USDT|USDT"}
    assert provenance["okx_selected_source_integrity"] == PIN
    assert provenance["okx_unselected_manifests"] == 1
    assert set(provenance["provider_sha256_by_month"]) == {"gate:2026-05", "okx:2026-05"}


def test_pinned_namespace_cannot_mislabel_other_source_manifest(tmp_path):
    from parlay_bot.research.archive_training import TrainingDataUnavailable

    _manifest(
        tmp_path / "source-families" / PIN / "verified_venue_perp_klines",
        "mismatch.manifest.json", venue="okx", integrity=OTHER,
    )
    with pytest.raises(TrainingDataUnavailable, match="wrong source identity"):
        archive_training.load_frames_manifests(
            tmp_path, okx_source_integrity=PIN,
        )


@pytest.mark.parametrize("days,expected_status", [
    (6, "incomplete"), (7, "measured"),
])
def test_train_only_report_preserves_real_inner_fold_shortfall(
    tmp_path, monkeypatch, days, expected_status,
):
    from tools import run_xmarket_train_only_research as research

    calls = []

    def load(_root, *, target_asset, okx_source_integrity):
        calls.append(("load", target_asset, okx_source_integrity))
        return {"opaque": True}, {
            "provider_sha256_by_month": {"okx:2026-05": "a" * 64},
            "okx_selected_source_integrity": okx_source_integrity,
            "okx_unselected_manifests": 1,
        }

    async def native(_asset):
        return {"verification": {
            "polymarket": {"status": "verified"},
            "kalshi": {"status": "verified"},
        }}

    def train(_frames, *, horizon, market_data, research_only, **_kwargs):
        assert research_only is True
        assert market_data["verification"]["kalshi"]["status"] == "verified"
        calls.append(("train", horizon))
        status = "measured_train_only" if days >= 7 else "insufficient_train_only_fold"
        return {"artifact": None, "publish_kind": "report", "report": {
            "training_status": "research_only",
            "train_only_feature_ablation": {
                "status": status, "fold": {"eval_days": days}, "profiles": [],
                "can_publish_model": False,
            },
            "overlap_evidence": {"eligible": True},
        }}

    monkeypatch.setattr(research, "load_frames_manifests", load)
    monkeypatch.setattr(research, "_read_originals", native)
    monkeypatch.setattr(research, "train_run", train)
    receipt = research.run(
        asset="BTC", manifests=tmp_path, okx_source_integrity=PIN,
        train_end="2026-09-19T00:00:00+00:00",
        test_start="2026-10-17T00:00:00+00:00",
        trained_through="2026-11-13T23:55:00+00:00",
    )
    assert receipt["status"] == expected_status
    assert receipt["research_only"] is True
    assert receipt["models_published"] == receipt["trades_placed"] == 0
    assert receipt["okx_selected_source_integrity"] == PIN
    assert receipt["okx_unselected_manifests"] == 1
    assert [s["horizon_minutes"] for s in receipt["scopes"]] == [5, 15, 60]
    assert [s["inner_train_only_eval_days"] for s in receipt["scopes"]] == [days] * 3
    assert [s["inner_train_only_missing_days"] for s in receipt["scopes"]] == [
        max(0, 7-days)
    ] * 3
    assert all(not s["outer_validation_test_scored"] for s in receipt["scopes"])
    assert calls == [("load", "BTC", PIN), ("train", 5), ("train", 15),
                     ("train", 60)]


def test_one_shot_workflow_selects_only_verified_okx_and_remains_nonrecurring():
    workflow = Path(".github/workflows/xmarket-train-only-research.yml").read_text()
    assert workflow.count("--okx-source-integrity okx-v5-public-history-5m-v1") == 2
    assert "schedule:" not in workflow
    assert "workflow_dispatch:" in workflow
    assert 'github.event_name == \'push\'' in workflow
    assert "python tools/run_xmarket_train_only_research.py" in workflow
