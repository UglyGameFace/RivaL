"""Batch 2 Phase 3 — research memory + autonomous loop (spec §3).

Tests cover the loop's wiring, not the numeric gates: ablation and
screen machinery are stubbed at their real function boundaries
(evaluate_hypothesis_ablations / verified_candidate /
FamilyPromotion.evaluate+selected), and receipt verification uses the
real verify_proposals(). The numeric gates themselves are covered by
test_llm_hypothesis_pipeline.py and test_research_candidate_registry.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.perpetuals.research_hypotheses import (
    MODEL,
    FeatureHypothesisLab,
)
from parlay_bot.perpetuals.research_loop import (
    ResearchLoop,
    create_experiment,
    get_experiment,
    list_findings,
    prior_trials_context,
    publish_kind,
    record_finding,
    record_stage_outcome,
    run_ablation,
    run_propose,
    run_screen,
    run_shadow_promotion,
    run_verify,
    supersede_experiment,
    transition,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SHA = "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path):
    return SQLiteHotStore(tmp_path / "loop.sqlite")


def _experiment(store, **kwargs):
    kwargs.setdefault("proposer", "human")
    kwargs.setdefault("hypothesis", "volume_only")
    kwargs.setdefault("design", {"asset": "BTC", "venues": ["hyperliquid"],
                                 "model_shape": "offline-live-contract-logistic-v3",
                                 "calibration_method": "platt-scaling-ece<=0.10"})
    kwargs.setdefault("now", NOW)
    return create_experiment(store, **kwargs)


def _passing_trial():
    return {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
            "horizon_minutes": 5, "hypothesis": "volume_only",
            "feature_indices": [4, 9], "proposal_sha256": SHA,
            "validation_brier_delta": 0.012, "holdout_brier_delta": 0.011,
            "holdout_examined": True,
            "retrospective_candidate_status": "screened_for_future_shadow"}


def _failing_trial():
    trial = _passing_trial()
    trial["holdout_brier_delta"] = 0.002
    trial["retrospective_candidate_status"] = "ordinary_holdout_not_beaten"
    return trial


def _evaluation(family="flow_v3", selected=None, status="held"):
    return {
        "status": status, "selected_family": selected,
        "families": {family: {
            "qualified": selected == family, "targets": 180, "days": 22,
            "challenger_trades": 61, "quote_coverage": 0.87, "ece": 0.06,
            "forward_gain": 0.009,
            "cost_proof": {"severe": {"paired_gain_bps": 3.1}}}},
    }


class _Promoter:
    """Stub at the FamilyPromotion.selected() boundary."""

    def __init__(self, selected):
        self._selected = selected

    def selected(self, **kwargs):
        return self._selected


def _valid_receipt():
    identity = {
        "asset": "BTC", "slot_utc": (NOW - timedelta(hours=1)).isoformat(),
        "model": MODEL, "evidence_sha256": SHA,
        "hypothesis": "volume_only", "venues": ["hyperliquid"],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"status": "verified", "source": "live_postgresql_llm_hypotheses",
            "proposals": [{**identity, "record_sha256": digest}]}


def test_full_lifecycle_in_order(tmp_path):
    store = _store(tmp_path)
    try:
        loop = ResearchLoop(store)
        exp = loop.create(proposer="human", hypothesis="volume_only",
                          design={"asset": "BTC"}, now=NOW)
        assert exp["status"] == "proposed"
        assert exp["record_sha256"]
        exp = loop.verify(exp["experiment_id"], _valid_receipt(),
                          venue="hyperliquid", now=NOW)
        assert exp["status"] == "ablating"
        exp = loop.ablate(exp["experiment_id"], trials=[_passing_trial()],
                          report_sha256=SHA, now=NOW)
        assert exp["status"] == "screened"
        exp = loop.screen(
            exp["experiment_id"], artifact=b"{}",
            screen=lambda artifact: {"retrospective_screen": "passed"},
            eligible=True, now=NOW)
        assert exp["status"] == "shadow"
        assert exp["outcome_json"][-1]["outcome"]["publish_kind"] == "model"
        # Shadow accumulates scores without moving status.
        exp = record_stage_outcome(
            store, exp["experiment_id"], stage="shadow",
            outcome={"targets": 90, "days": 12}, now=NOW)
        assert exp["status"] == "shadow"
        exp = loop.shadow(
            exp["experiment_id"], family="flow_v3",
            evaluation=_evaluation(selected="flow_v3", status="promoted_paper"),
            promoter=_Promoter("flow_v3"), asset="BTC", venue="hyperliquid",
            contract="BTC", quote="USDC", horizon=5, now=NOW)
        assert exp["status"] == "promoted"
        findings = loop.findings(exp["experiment_id"])
        assert len(findings) == 1 and findings[0]["kind"] == "discovery"
        assert findings[0]["summary"]
        # Events are append-only: every stage is still in the log.
        stages = [event["stage"] for event in exp["outcome_json"]]
        assert stages == ["proposed", "ablating", "screened", "shadow",
                          "shadow", "promoted"]
    finally:
        store.close()


def test_receipt_verification_failure_ends_loop_failed(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        out = run_verify(store, exp["experiment_id"], {"status": "unverified"},
                         venue="hyperliquid", now=NOW)
        assert out["status"] == "failed"
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
        assert findings[0]["recorded_by"] == "system"
    finally:
        store.close()


def test_ablation_failure_discards_with_finding(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        run_verify(store, exp["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        out = run_ablation(store, exp["experiment_id"],
                           trials=[_failing_trial()], report_sha256=SHA, now=NOW)
        assert out["status"] == "discarded"
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
        # A raising ablation also discards, never leaves the loop hanging.
        exp2 = _experiment(store)
        run_verify(store, exp2["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        out2 = run_ablation(store, exp2["experiment_id"],
                            ablate=lambda **kw: (_ for _ in ()).throw(
                                RuntimeError("offline job crashed")),
                            now=NOW)
        assert out2["status"] == "discarded"
        assert len(list_findings(store, exp2["experiment_id"])) == 1
    finally:
        store.close()


def test_screen_failure_discards_with_finding(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        run_verify(store, exp["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        run_ablation(store, exp["experiment_id"], trials=[_passing_trial()],
                     report_sha256=SHA, now=NOW)
        out = run_screen(
            store, exp["experiment_id"], artifact=b"{}",
            screen=lambda artifact: {"retrospective_screen": "insufficient",
                                     "admission_reason": "too_few_months"},
            eligible=False, now=NOW)
        assert out["status"] == "discarded"
        assert out["outcome_json"][-1]["outcome"]["publish_kind"] == "report"
        assert out["outcome_json"][-1]["outcome"]["promoted_live"] is False
        assert len(list_findings(store, exp["experiment_id"])) == 1
        # A raising screen also discards.
        exp2 = _experiment(store)
        run_verify(store, exp2["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        run_ablation(store, exp2["experiment_id"], trials=[_passing_trial()],
                     report_sha256=SHA, now=NOW)
        out2 = run_screen(store, exp2["experiment_id"], artifact=b"not-json",
                          now=NOW)
        assert out2["status"] == "discarded"
    finally:
        store.close()


def test_lookback_expiry_discards_and_demotion_is_recorded(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        run_verify(store, exp["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        run_ablation(store, exp["experiment_id"], trials=[_passing_trial()],
                     report_sha256=SHA, now=NOW)
        run_screen(store, exp["experiment_id"], artifact=b"{}",
                   screen=lambda artifact: {"retrospective_screen": "passed"},
                   now=NOW)
        out = run_shadow_promotion(
            store, exp["experiment_id"], family="flow_v3",
            evaluation=_evaluation(), lookback_expired=True, now=NOW)
        assert out["status"] == "discarded"
        assert len(list_findings(store, exp["experiment_id"])) == 1

        # Demotion: promoted, then re-evaluation fails.
        exp2 = _experiment(store)
        run_verify(store, exp2["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        run_ablation(store, exp2["experiment_id"], trials=[_passing_trial()],
                     report_sha256=SHA, now=NOW)
        run_screen(store, exp2["experiment_id"], artifact=b"{}",
                   screen=lambda artifact: {"retrospective_screen": "passed"},
                   now=NOW)
        run_shadow_promotion(
            store, exp2["experiment_id"], family="flow_v3",
            evaluation=_evaluation(selected="flow_v3", status="promoted_paper"),
            promoter=_Promoter("flow_v3"), asset="BTC", venue="hyperliquid",
            contract="BTC", quote="USDC", horizon=5, now=NOW)
        assert get_experiment(store, exp2["experiment_id"])["status"] == "promoted"
        out2 = run_shadow_promotion(
            store, exp2["experiment_id"], family="flow_v3",
            evaluation=_evaluation(selected=None, status="demoted"),
            promoter=_Promoter(None), asset="BTC", venue="hyperliquid",
            contract="BTC", quote="USDC", horizon=5, now=NOW)
        assert out2["status"] == "discarded"
        assert out2["outcome_json"][-1]["outcome"]["demoted"] is True
        assert out2["outcome_json"][-1]["outcome"]["targets"] == 180
        findings = list_findings(store, exp2["experiment_id"])
        assert findings[-1]["kind"] == "mistake"
    finally:
        store.close()


def test_close_without_finding_is_refused_and_illegal_steps_refused(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        finding = {"kind": "negative_result", "summary": "Receipt bad.",
                   "evidence_ref": "x", "recorded_by": "system"}
        # Terminal close without a finding is an error, not silent.
        with pytest.raises(ValueError, match="requires a finding"):
            transition(store, exp["experiment_id"], "failed", actor="system",
                       now=NOW)
        # With a finding it closes.
        out = transition(store, exp["experiment_id"], "failed", actor="system",
                         finding=finding, now=NOW)
        assert out["status"] == "failed"
        # Terminal states accept no further transition.
        with pytest.raises(ValueError, match="illegal experiment transition"):
            transition(store, exp["experiment_id"], "ablating", actor="system",
                       now=NOW)
        # Skipping stages is refused.
        exp2 = _experiment(store)
        with pytest.raises(ValueError, match="illegal experiment transition"):
            transition(store, exp2["experiment_id"], "shadow", actor="system",
                       now=NOW)
        # A finding on a closed experiment's stage outcome is refused.
        with pytest.raises(ValueError):
            record_stage_outcome(store, exp["experiment_id"], stage="shadow",
                                 outcome={"targets": 1}, now=NOW)
    finally:
        store.close()


def test_append_only_no_delete_and_events_never_shrink(tmp_path):
    source = (ROOT / "src" / "parlay_bot" / "perpetuals" / "research_loop.py"
              ).read_text()
    assert "DELETE FROM" not in source
    assert "DELETE " not in source.replace("no DELETE", "")
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        first_events = list(exp["outcome_json"])
        run_verify(store, exp["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        later = get_experiment(store, exp["experiment_id"])
        assert later["outcome_json"][:1] == first_events
        assert len(later["outcome_json"]) == len(first_events) + 1
        # Corrections supersede; the old row keeps its history.
        replacement = _experiment(store, proposer="human",
                                  hypothesis="trend_only")
        old = supersede_experiment(store, exp["experiment_id"],
                                   new_experiment=replacement, now=NOW)
        assert old["superseded_by"] == replacement["experiment_id"]
        assert old["outcome_json"] == later["outcome_json"]
        assert old["status"] == "ablating"
    finally:
        store.close()


def test_llm_output_cannot_drive_a_transition(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        with pytest.raises(ValueError, match="LLM output cannot drive"):
            transition(store, exp["experiment_id"], "ablating", actor="llm",
                       now=NOW)
        with pytest.raises(ValueError, match="LLM output cannot drive"):
            transition(store, exp["experiment_id"], "ablating",
                       actor={"llm_decision": "promote"}, now=NOW)
        assert get_experiment(store, exp["experiment_id"])["status"] == "proposed"
    finally:
        store.close()


def test_prior_trials_context_has_numbers_only(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        run_verify(store, exp["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        run_ablation(store, exp["experiment_id"], trials=[_passing_trial()],
                     report_sha256=SHA, now=NOW)
        run_screen(store, exp["experiment_id"], artifact=b"{}",
                   screen=lambda artifact: {"retrospective_screen": "passed"},
                   now=NOW)
        run_shadow_promotion(
            store, exp["experiment_id"], family="flow_v3",
            evaluation=_evaluation(selected="flow_v3", status="promoted_paper"),
            promoter=_Promoter("flow_v3"), asset="BTC", venue="hyperliquid",
            contract="BTC", quote="USDC", horizon=5, now=NOW)
        assert get_experiment(store, exp["experiment_id"])["status"] == "promoted"
        context = prior_trials_context(store, asset="BTC", now=NOW)
        assert context, "90-day experiments must reach the next proposal"
        blob = json.dumps(context)
        assert "promoted" not in blob
        assert "discarded" not in blob and "failed" not in blob
        assert '"status"' not in blob
        entry = next(e for e in context if e["hypothesis"] == "volume_only")
        assert entry["holdout_brier_delta"] == 0.011
        # Experiments older than 90 days are out of the window.
        old = _experiment(store, now=NOW - timedelta(days=91))
        context2 = prior_trials_context(store, asset="BTC", now=NOW)
        assert all(e.get("experiment_id") != old["experiment_id"]
                   for e in context2)
        assert publish_kind(eligible=True) == "model"
        assert publish_kind(eligible=False) == "report"
    finally:
        store.close()


def test_finding_validation(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        with pytest.raises(ValueError):
            record_finding(store, experiment_id=exp["experiment_id"],
                           kind="discovery", summary="x" * 281,
                           evidence_ref="ref", recorded_by="system", now=NOW)
        with pytest.raises(ValueError):
            record_finding(store, experiment_id=exp["experiment_id"],
                           kind="guess", summary="ok",
                           evidence_ref="ref", recorded_by="system", now=NOW)
        with pytest.raises(ValueError):
            record_finding(store, experiment_id="missing",
                           kind="discovery", summary="ok",
                           evidence_ref="ref", recorded_by="system", now=NOW)
        found = record_finding(store, experiment_id=exp["experiment_id"],
                               kind="discovery", summary="Volume mask helped.",
                               evidence_ref="rival_llm_hypothesis_trials:x",
                               recorded_by="human", now=NOW)
        assert found["finding_id"]
    finally:
        store.close()


class _FakeAI:
    def __init__(self, reply=None, asset="BTC"):
        self.available = True
        self.calls = 0
        self.reply = reply or {
            "model": MODEL, "asset": asset, "hypothesis": "volume_only",
            "rationale": "Compare native volume features",
            "falsification": "Reject worse validation Brier",
            "research_only": True, "retrospective_screen": "not_tested",
            "live_model_promoted": False, "prediction": None,
            "evidence_sha256": SHA,
            "venues": ["hyperliquid"],
        }

    async def propose_feature_hypothesis(self, **kwargs):
        self.calls += 1
        return dict(self.reply)


def test_opt_in_false_blocks_automatic_loop_llm_calls(tmp_path):
    store = _store(tmp_path)
    try:
        ai = _FakeAI()
        out = asyncio.run(run_propose(
            store, asset="BTC", venues=("hyperliquid",), ai=ai,
            background=True, background_opt_in=False, now=NOW))
        assert out["status"] == "opt_in_required"
        assert ai.calls == 0
        # Opted in: the call happens and an experiment opens.
        out = asyncio.run(run_propose(
            store, asset="BTC", venues=("hyperliquid",), ai=ai,
            background=True, background_opt_in=True, now=NOW))
        assert out["status"] == "proposed" and ai.calls == 1
        assert get_experiment(store, out["experiment_id"])["status"] == "proposed"
        # User-initiated proposals do not consult the opt-in flag.
        ai2 = _FakeAI(asset="ETH")
        out = asyncio.run(run_propose(
            store, asset="ETH", venues=("hyperliquid",), ai=ai2,
            background=False, background_opt_in=False, now=NOW))
        assert out["status"] == "proposed" and ai2.calls == 1
        # An invalid proposal is refused, never stored.
        bad = _FakeAI(reply={"model": MODEL, "asset": "BTC",
                             "hypothesis": "not_a_hypothesis",
                             "rationale": "x" * 10, "falsification": "y" * 10,
                             "research_only": True,
                             "retrospective_screen": "not_tested",
                             "live_model_promoted": False, "prediction": None,
                             "venues": ["hyperliquid"]})
        with pytest.raises(ValueError, match="invalid LFM3 hypothesis"):
            asyncio.run(run_propose(
                store, asset="BTC", venues=("hyperliquid",), ai=bad,
                background=False, background_opt_in=False, now=NOW))
        assert bad.calls == 1
    finally:
        store.close()


def test_lab_proposal_opens_an_experiment(tmp_path):
    store = _store(tmp_path)
    try:
        ai = _FakeAI()
        lab = FeatureHypothesisLab(store, ai, venues=("hyperliquid",))
        result = asyncio.run(lab.run_once(now=datetime(2026, 11, 6, 0, 0,
                                                       tzinfo=UTC)))
        assert result["status"] == "proposed"
        assert result["asset"] == "BTC"  # 00:00 UTC slot rotates to BTC
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rival_research_experiments").fetchall()
        assert len(rows) == 1
        assert rows[0]["proposal_sha256"] == result["record_sha256"]
        assert rows[0]["status"] == "proposed"
        assert json.loads(rows[0]["design_json"])["asset"] == "BTC"
    finally:
        store.close()


def test_status_card_surfaces_promoted_experiments(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store)
        run_verify(store, exp["experiment_id"], _valid_receipt(),
                   venue="hyperliquid", now=NOW)
        run_ablation(store, exp["experiment_id"], trials=[_passing_trial()],
                     report_sha256=SHA, now=NOW)
        run_screen(store, exp["experiment_id"], artifact=b"{}",
                   screen=lambda artifact: {"retrospective_screen": "passed"},
                   now=NOW)
        run_shadow_promotion(
            store, exp["experiment_id"], family="flow_v3",
            evaluation=_evaluation(selected="flow_v3", status="promoted_paper"),
            promoter=_Promoter("flow_v3"), asset="BTC", venue="hyperliquid",
            contract="BTC", quote="USDC", horizon=5, now=NOW)
        alerts = research_alerts(store, now=NOW)
        keys = [key for key, _ in alerts]
        assert any(key.startswith("research-experiment:promoted:")
                   for key in keys)
        text = next(t for k, t in alerts
                    if k.startswith("research-experiment:promoted:"))
        assert "volume_only" in text
    finally:
        store.close()
