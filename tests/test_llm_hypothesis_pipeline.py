"""LFM3 hypotheses are bounded, persistent, and tested without holdout leakage."""
from __future__ import annotations

import hashlib
import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parlay_bot.perpetuals.research_hypotheses import (
    ASSETS,
    FeatureHypothesisLab,
    recent_hypotheses,
    validate_proposal,
)
from parlay_bot.research import llm_feature_ablation as ablation
from parlay_bot.storage.hot import SQLiteHotStore

ROOT = Path(__file__).resolve().parents[1]
RELAY = runpy.run_path(str(ROOT / "tools/discloud_feature_hypotheses.py"))
MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"


def proposal(asset, mode="trend_only"):
    return {
        "asset": asset, "model": MODEL, "hypothesis": mode,
        "rationale": "Compare only fixed, original indicators",
        "falsification": "Reject when chronological validation degrades",
        "research_only": True,
        "venues": ["okx", "gate"],
        "evidence_sha256": "a" * 64,
    }


def service_proposal():
    return {
        "model": MODEL, "hypothesis": "trend_only",
        "rationale": "Compare native trend features",
        "falsification": "Reject a worse Brier score",
        "research_only": True, "retrospective_screen": "not_tested",
        "live_model_promoted": False, "prediction": None,
    }


def test_feature_hypothesis_output_contract_rejects_trading_claims():
    assert validate_proposal(service_proposal())
    for field, change in (
        ("model", "unapproved"),
        ("hypothesis", "trade_now"),
        ("research_only", False),
        ("live_model_promoted", True),
        ("prediction", {"price": 123}),
        ("retrospective_screen", "passed"),
    ):
        assert not validate_proposal({**service_proposal(), field: change})


@pytest.mark.asyncio
async def test_four_asset_slots_dedupe_and_only_verified_proposals(tmp_path):
    store = SQLiteHotStore(tmp_path / "hypotheses.sqlite")
    calls = []

    class FakeAI:
        available = True

        async def propose_feature_hypothesis(self, *, asset, venues):
            assert "okx" in venues
            calls.append(asset)
            return proposal(asset, mode="volume_only")

    lab = FeatureHypothesisLab(store, FakeAI(), venues=("okx", "gate"))
    try:
        for hour, asset in zip((0, 6, 12, 18), ASSETS):
            stamp = datetime(2026, 11, 2, hour, 10, tzinfo=UTC)
            got = await lab.run_once(now=stamp)
            assert got["status"] == "proposed"
            assert got["asset"] == asset
            assert (await lab.run_once(now=stamp))["status"] == "already_attempted"
        assert calls == list(ASSETS)
        observed = recent_hypotheses(
            store, venue="okx", now=datetime(2026, 11, 3, tzinfo=UTC),
        )
        assert len(observed) == 4
        assert {x["asset"] for x in observed} == set(ASSETS)
        assert recent_hypotheses(
            store, venue="bitget", now=datetime(2026, 11, 3, tzinfo=UTC),
        ) == []
        receipt = {
            "status": "verified", "source": "live_postgresql_llm_hypotheses",
            "proposals": observed,
        }
        assert len(ablation.verify_proposals(
            receipt, venue="okx", now=datetime(2026, 11, 3, tzinfo=UTC),
        )) == 4
        changed = [{**observed[0], "hypothesis": "momentum_only"}, *observed[1:]]
        with pytest.raises(ValueError, match="digest mismatch"):
            ablation.verify_proposals(
                {**receipt, "proposals": changed}, venue="okx",
                now=datetime(2026, 11, 3, tzinfo=UTC),
            )
        assert not ablation.verify_proposals(
            {"status": "unverified", "proposals": observed}, venue="okx",
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_llm_request_does_not_duplicate_spend_or_invent_model(tmp_path):
    store = SQLiteHotStore(tmp_path / "fail.sqlite")
    class Missing:
        available = False

    lab = FeatureHypothesisLab(store, Missing(), venues=("okx",))
    now = datetime(2026, 11, 2, 7, 20, tzinfo=UTC)
    try:
        assert (await lab.run_once(now=now))["status"] == "local_research_not_configured"
        assert (await lab.run_once(now=now))["status"] == "already_attempted"
        assert recent_hypotheses(
            store, venue="okx", now=now + timedelta(days=1),
        ) == []
    finally:
        store.close()


def test_offline_ablation_uses_sealed_source_and_reports_no_trading_gain(monkeypatch):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    row = {
        "asset": "BTC", "slot_utc": "2026-11-02T00:00:00+00:00",
        "model": MODEL, "evidence_sha256": "f" * 64,
        "hypothesis": "momentum_only", "venues": ["okx"],
    }
    row["record_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in row.items() if k != "record_sha256"},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    receipt = {"source": "live_postgresql_llm_hypotheses",
               "status": "verified", "proposals": [row]}
    accepted = ablation.verify_proposals(receipt, venue="okx", now=now)
    from parlay_bot.research import llm_feature_ablation as mod

    monkeypatch.setattr(mod, "qualified_manifest_groups",
                        lambda root, *, qualified_streams: [["one"], ["two"]])
    def fake_assemble(paths):
        label = "BTC-USDT-SWAP" if paths == ["one"] else "ETH-USDT-SWAP"
        return ({
            "venue": "okx", "contract": label, "quote": "USDT",
            "interval_minutes": 5,
        }, [], {"months": [f"2026-{m:02d}" for m in range(5, 11)]})
    monkeypatch.setattr(mod, "assemble", fake_assemble)
    monkeypatch.setattr(mod, "historical_features", lambda rows, identity: {})
    seen = []
    def partitions(rows, vectors, *, horizon, identity):
        seen.append(horizon)
        training = [(now, now, (0.2,) * 10, 1)] * 300
        validating = [(now, now, (0.1,) * 10, 0)] * 101
        class SealedHoldout:
            def __iter__(self):
                raise AssertionError("holdout must never be scored")
        return training, validating, SealedHoldout()
    monkeypatch.setattr(mod, "compatible_partitions", partitions)
    comparisons = ablation.evaluate_hypothesis_ablations(
        None, qualified_streams=set(), proposals=accepted,
    )
    assert len(comparisons) == len(seen) == 3
    assert {t["asset"] for t in comparisons} == {"BTC"}
    assert all(t["holdout_examined"] is False for t in comparisons)
    assert all(t["candidate_imported"] is False for t in comparisons)
    assert all(t["after_cost_edge_verified"] is False for t in comparisons)
    assert {t["hypothesis"] for t in comparisons} == {"momentum_only"}


def test_relay_never_reads_host_without_discloud_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("DISCLOUD_APP_ID", raising=False)
    path = tmp_path / "proposals.json"
    assert RELAY["main"](["--venue", "okx", "--output", str(path)]) == 0
    assert json.loads(path.read_text()) == {
        "status": "unverified", "proposals": [],
    }
