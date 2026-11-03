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
    hypothesis_attempt_status,
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


@pytest.mark.asyncio
async def test_transient_llm_outage_retries_with_bounded_slot_budget(tmp_path):
    store = SQLiteHotStore(tmp_path / "bounded-retry.sqlite")
    class Flaky:
        available = True
        calls = 0

        async def propose_feature_hypothesis(self, *, asset, venues):
            self.calls += 1
            if self.calls < 3:
                from parlay_bot.perpetuals.ai_types import ResearchUnavailable
                raise ResearchUnavailable("temporary local CPU saturation")
            return {**proposal(asset), "venues": ["okx"]}

    lab = FeatureHypothesisLab(store, Flaky(), venues=("okx",))
    begin = datetime(2026, 11, 2, 7, 1, tzinfo=UTC)
    try:
        assert (await lab.run_once(now=begin))["status"] == "unavailable"
        assert (await lab.run_once(now=begin+timedelta(minutes=19)))["status"] == "already_attempted"
        assert (await lab.run_once(now=begin+timedelta(minutes=20)))["status"] == "unavailable"
        assert (await lab.run_once(now=begin+timedelta(minutes=40)))["status"] == "proposed"
        assert (await lab.run_once(now=begin+timedelta(minutes=60)))["status"] == "already_attempted"
        info = hypothesis_attempt_status(
            store, now=begin+timedelta(minutes=60),
        )
        assert info["status"] == "recorded"
        assert info["latest_by_asset"][0]["asset"] == "ETH"
        assert info["latest_by_asset"][0]["attempt_count"] == 3
        assert info["latest_by_asset"][0]["status"] == "proposed"
        assert lab.ai.calls == 3
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_llm_feature_hypotheses"
            ).fetchone()["n"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unconfigured_slot_can_recover_after_configuration(tmp_path):
    store = SQLiteHotStore(tmp_path / "recover-config.sqlite")
    class Config:
        available = False
        async def propose_feature_hypothesis(self, *, asset, venues):
            return {**proposal(asset), "venues": ["okx"]}

    provider = Config()
    lab = FeatureHypothesisLab(store, provider, venues=("okx",))
    start = datetime(2026, 11, 2, 12, 5, tzinfo=UTC)
    try:
        assert (await lab.run_once(now=start))["status"] == "local_research_not_configured"
        provider.available = True
        assert (await lab.run_once(now=start+timedelta(minutes=20)))["status"] == "proposed"
        assert (await lab.run_once(now=start+timedelta(minutes=40)))["status"] == "already_attempted"
    finally:
        store.close()


def test_llm_mask_qualifies_only_after_purged_validation_and_sealed_test(monkeypatch):
    """One LFM3 mask can create a hash-screened *shadow* candidate, never a win."""
    from parlay_bot.perpetuals.model_checkpoints import canonical
    from parlay_bot.research import llm_feature_ablation as mod
    from parlay_bot.research.candidate_registry import verified_candidate

    months = [f"2026-{month:02d}" for month in range(5, 11)]
    identity = {
        "venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT",
        "interval_minutes": 5,
    }
    provenance = {
        "months": months,
        "source_sha256_by_month": {month: "a"*64 for month in months},
    }
    monkeypatch.setattr(
        mod, "qualified_manifest_groups",
        lambda root, *, qualified_streams: [["verified-source"]],
    )
    monkeypatch.setattr(
        mod, "assemble", lambda paths: (identity, [{"source": 1}], provenance),
    )
    monkeypatch.setattr(mod, "historical_features", lambda rows, identity: {})
    stamp = datetime(2026, 5, 1, tzinfo=UTC)
    def sample(days):
        date = stamp + timedelta(days=days)
        return (date, date+timedelta(minutes=5), (.1,)*10, 1)
    train = [sample(1)] * 310
    validation = [sample(130)] * 120
    holdout = [sample(150)] * 120
    monkeypatch.setattr(
        mod, "compatible_partitions",
        lambda rows, vectors, *, horizon, identity: (train, validation, holdout),
    )
    def fit(rows, *, l2, rate=.0125, epochs=4, active=None):
        return ([.1 if active is None else .2]*11, .5)
    monkeypatch.setattr(mod, "_fit", fit)
    def score(rows, weights, base):
        brier = .18 if weights[0] == .2 else .23
        return {
            "count": len(rows), "days": 21, "brier": brier,
            "baseline_brier": .25, "brier_gain": .25-brier,
            "accuracy": .7, "ece_10": .02,
        }
    monkeypatch.setattr(mod, "_score", score)
    proposal_row = {
        "asset": "BTC", "slot_utc": "2026-11-02T00:00:00+00:00",
        "model": MODEL, "evidence_sha256": "f"*64,
        "hypothesis": "volatility_only", "venues": ["okx"],
    }
    proposal_row["record_sha256"] = hashlib.sha256(json.dumps(
        proposal_row, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    candidates = []
    trials = mod.evaluate_hypothesis_ablations(
        None, qualified_streams=set(), proposals=[proposal_row],
        candidate_sink=candidates,
    )
    assert len(trials) == len(candidates) == 3
    assert all(t["passed_purged_validation"] and t["holdout_examined"]
               and t["retrospective_candidate_status"] ==
                   "screened_for_future_shadow" for t in trials)
    for model in candidates:
        assert model["research_candidate_passed"]
        assert not model["live_model_promoted"]
        assert model["model_spec"]["method"] == "lfm3-hypothesis-volatility_only"
        assert model["model_spec"]["lfm3_proposal_sha256"] == proposal_row["record_sha256"]
        assert verified_candidate(
            canonical({
                "research_only": model["research_only"],
                "live_model_promoted": model["live_model_promoted"],
                "model_sha256": model["model_sha256"],
                "model_spec": model["model_spec"],
                "validation": model["validation"],
                "out_of_sample_test": model["out_of_sample_test"],
            }).encode(),
        )["retrospective_screen"] == "passed"


def test_weak_llm_mask_never_touches_sealed_holdout(monkeypatch):
    """A feature mask losing to ordinary logit stays validation-only."""
    from parlay_bot.research import llm_feature_ablation as mod

    monkeypatch.setattr(
        mod, "qualified_manifest_groups",
        lambda root, *, qualified_streams: [["one"]],
    )
    monkeypatch.setattr(
        mod, "assemble", lambda paths: (
            {"venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT",
             "interval_minutes": 5}, [{"source": 1}],
            {"months": [f"2026-{m:02d}" for m in range(5, 11)]},
        ),
    )
    monkeypatch.setattr(mod, "historical_features", lambda rows, identity: {})
    now = datetime(2026, 7, 1, tzinfo=UTC)
    record = (now, now+timedelta(minutes=5), (.1,)*10, 1)
    class Sealed:
        def __iter__(self):
            raise AssertionError("sealed outcomes consulted before validation")
        def __getitem__(self, index):
            raise AssertionError("sealed outcomes indexed before validation")
    monkeypatch.setattr(
        mod, "compatible_partitions",
        lambda rows, vectors, *, horizon, identity: (
            [record]*310, [record]*120, Sealed(),
        ),
    )
    monkeypatch.setattr(
        mod, "_fit", lambda rows, *, l2, rate=.0125, epochs=4, active=None:
            ([.1 if active is None else .2]*11, .5),
    )
    monkeypatch.setattr(
        mod, "_score", lambda rows, weights, base: {
            "count": 120, "days": 21,
            "brier": .20 if weights[0] == .1 else .24,
            "baseline_brier": .25, "brier_gain": .01,
            "accuracy": .6, "ece_10": .03,
        },
    )
    proposal_row = {
        "asset": "BTC", "slot_utc": "2026-11-02T00:00:00+00:00",
        "model": MODEL, "evidence_sha256": "f"*64,
        "hypothesis": "volume_only", "venues": ["okx"],
    }
    proposal_row["record_sha256"] = hashlib.sha256(json.dumps(
        proposal_row, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    candidates = []
    trials = mod.evaluate_hypothesis_ablations(
        None, qualified_streams=set(), proposals=[proposal_row],
        candidate_sink=candidates,
    )
    assert len(trials) == 3
    assert not candidates
    assert all(t["holdout_examined"] is False for t in trials)
    assert all(t["retrospective_candidate_status"] == "validation_rejected"
               for t in trials)
