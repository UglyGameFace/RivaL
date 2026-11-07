"""Autonomous-loop amendments D15-D18 — follow-up on the merged driver.

Tests cover the four Phase 1-scope amendments against the merged driver
(main 611c553): oldest-first proposal selection + 9-day window + expiry
close (D15), split capacity pools (D16), outcome receipts + apply-once
(D17), and card progress + blockers (D18). D14 pinning and the shared
GBM workflow are Phase 2 scope and are NOT tested here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.perpetuals.research_hypotheses import (
    MODEL,
    FeatureHypothesisLab,
    list_blocked_receipts,
    recent_hypotheses,
    record_blocked_receipt,
    record_trials,
)
from parlay_bot.perpetuals.research_loop import (
    create_experiment,
    get_experiment,
    list_findings,
    pool_counts,
    recent_experiments,
    transition,
)
from parlay_bot.research.llm_feature_ablation import verify_proposals
from parlay_bot.research.research_loop_driver import (
    advance_one,
    is_report_consumed,
    record_consumed_report,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SHA = "a" * 64
OTHER_SHA = "b" * 64


def _store(tmp_path):
    return SQLiteHotStore(tmp_path / "amend.sqlite")


def _digest(*, slot: datetime, hypothesis: str = "volume_only") -> str:
    identity = {
        "asset": "BTC", "slot_utc": slot.isoformat(), "model": MODEL,
        "evidence_sha256": SHA, "hypothesis": hypothesis,
        "venues": ["hyperliquid"],
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _seed_proposal(store, *, digest: str, slot: datetime) -> None:
    lab = FeatureHypothesisLab(store, None, venues=("hyperliquid",))
    lab.initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_llm_feature_hypotheses
               (asset, slot_utc, attempted_at, evidence_sha256, model,
                hypothesis, feature_indices_json, rationale, falsification,
                source_venues_json, record_sha256, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("BTC", slot.isoformat(), slot.isoformat(), SHA, MODEL,
             "volume_only", None, "rationale text", "falsification text",
             json.dumps(["hyperliquid"], separators=(",", ":")),
             digest, "proposed"),
        )


def _experiment(store, *, digest: str, now: datetime = NOW,
                venues: tuple[str, ...] = ("hyperliquid",)):
    return create_experiment(
        store, proposer="llm", hypothesis="volume_only",
        design={"asset": "BTC", "venues": list(venues),
                "model_shape": "offline-live-contract-logistic-v3",
                "calibration_method": "platt-scaling-ece<=0.10"},
        proposal_sha256=digest, now=now,
    )


def _passing_trial(*, sha: str = SHA):
    return {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
            "horizon_minutes": 5, "hypothesis": "volume_only",
            "feature_indices": [4, 9], "proposal_sha256": sha,
            "validation_count": 200,
            "baseline_validation_brier": 0.25,
            "llm_mask_validation_brier": 0.238,
            "validation_brier_delta": 0.012, "holdout_brier_delta": 0.011,
            "holdout_examined": True,
            "retrospective_candidate_status": "screened_for_future_shadow"}


def _trial_report(*, trials) -> bytes:
    return json.dumps({
        "schema_version": "rival-native-source-research-v1",
        "venue": "hyperliquid",
        "llm_hypothesis_research": {"experiments": trials},
    }).encode()


def _candidate_payload(*, proposal_sha: str = SHA):
    from parlay_bot.perpetuals.model_checkpoints import (
        FEATURE_CONTRACT,
        TARGET_CONTRACT,
    )

    spec = {
        "model": "offline-live-contract-logistic-v3",
        "method": "lfm3-hypothesis-volume_only",
        "identity": {"venue": "hyperliquid", "contract": "BTC",
                     "quote": "USDC", "interval_minutes": 5},
        "horizon_minutes": 5,
        "feature_contract": FEATURE_CONTRACT,
        "target_contract": TARGET_CONTRACT,
        "weights": [0.01] * 11,
        "train_base_rate": 0.5,
        "training_count": 500,
        "training_cutoff": "2026-09-01T00:00:00+00:00",
        "validation_cutoff": "2026-10-01T00:00:00+00:00",
        "evaluation_completed_through": "2026-11-01T00:00:00+00:00",
        "source_sha256_by_month": {f"2026-{m:02d}": "c" * 64
                                   for m in range(1, 7)},
        "provider_sha256_by_month": {},
        "months_without_provider_hash": [f"2026-{m:02d}" for m in range(1, 7)],
        "lfm3_proposal_sha256": proposal_sha,
        "lfm3_model": MODEL,
    }
    model_sha256 = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return json.dumps({
        "research_only": True, "live_model_promoted": False,
        "model_sha256": model_sha256, "model_spec": spec,
        "validation": {"count": 200, "brier": 0.20, "baseline_brier": 0.25,
                       "brier_gain": 0.05, "accuracy": 0.55, "ece_10": 0.05,
                       "days": 10},
        "out_of_sample_test": {"count": 200, "brier": 0.21,
                               "baseline_brier": 0.25, "brier_gain": 0.04,
                               "accuracy": 0.54, "ece_10": 0.06, "days": 10},
    }).encode()


def _seed_promotion_row(store, *, family: str, targets: int, days: int,
                        qualified: bool, evaluated_at: datetime,
                        horizon_minutes: int = 5):
    from parlay_bot.perpetuals.family_governance import FamilyPromotion

    FamilyPromotion(store).initialize()
    evidence = {family: {"qualified": qualified, "targets": targets,
                         "days": days, "challenger_trades": 61,
                         "quote_coverage": 0.87, "ece": 0.06,
                         "forward_gain": 0.009}}
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue, contract, quote, asset, horizon_minutes, evaluated_at,
                selected_family, status, reason, evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", horizon_minutes,
             evaluated_at.isoformat(),
             family if qualified else None,
             "promoted_paper" if qualified else "held", "test",
             json.dumps(evidence)),
        )


def _seed_receipt(store, *, family: str, recorded_at: datetime):
    from parlay_bot.perpetuals.promotion_receipts import initialize

    initialize(store)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion_receipts
               (family, venue, contract, quote, horizon_minutes, kind,
                evaluated_at, receipt_text, evidence_json, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (family, "hyperliquid", "BTC", "USDC", 5, "promoted",
             recorded_at.isoformat(), f"{family} promoted on BTC 5m",
             json.dumps({}), recorded_at.isoformat()),
        )


def _to_shadow(store, exp_id: str, *, now: datetime = NOW) -> None:
    transition(store, exp_id, "ablating", actor="system", now=now)
    transition(store, exp_id, "screened", actor="system", now=now)
    transition(store, exp_id, "shadow", actor="system", now=now)


# --- D15: oldest first + 9-day window + expiry close -----------------------


def test_recent_hypotheses_returns_oldest_per_asset(tmp_path):
    store = _store(tmp_path)
    try:
        older = _digest(slot=NOW - timedelta(days=2))
        newer = _digest(slot=NOW - timedelta(days=1))
        _seed_proposal(store, digest=older, slot=NOW - timedelta(days=2))
        _seed_proposal(store, digest=newer, slot=NOW - timedelta(days=1))
        rows = recent_hypotheses(store, venue="hyperliquid", now=NOW)
        assert [row["record_sha256"] for row in rows] == [older]
    finally:
        store.close()


def test_verify_proposals_9_day_window(tmp_path):
    store = _store(tmp_path)
    try:
        def receipt_for(slot: datetime) -> dict:
            digest = _digest(slot=slot)
            return {"status": "verified",
                    "source": "live_postgresql_llm_hypotheses",
                    "proposals": [{
                        "asset": "BTC", "slot_utc": slot.isoformat(),
                        "model": MODEL, "evidence_sha256": SHA,
                        "hypothesis": "volume_only",
                        "venues": ["hyperliquid"],
                        "record_sha256": digest}]}
        # 8 days old: inside the 9-day window (would have expired at 7).
        accepted = verify_proposals(
            receipt_for(NOW - timedelta(days=8)),
            venue="hyperliquid", now=NOW)
        assert len(accepted) == 1
        # 10 days old: outside the window.
        with pytest.raises(ValueError, match="stale or future AI proposal"):
            verify_proposals(
                receipt_for(NOW - timedelta(days=10)),
                venue="hyperliquid", now=NOW)
    finally:
        store.close()


def test_expired_proposal_closes_as_discarded_with_finding(tmp_path):
    store = _store(tmp_path)
    try:
        slot = NOW - timedelta(days=10)
        digest = _digest(slot=slot)
        _seed_proposal(store, digest=digest, slot=slot)
        exp = _experiment(store, digest=digest, now=NOW)
        out = advance_one(store, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "discarded", out
        assert get_experiment(store, exp["experiment_id"])["status"] == "discarded"
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert "expired" in findings[0]["summary"].lower()
        assert "9 days" in findings[0]["summary"]
    finally:
        store.close()


# --- D16: split capacity pools ---------------------------------------------


def test_pool_counts_split_offline_and_shadow(tmp_path):
    store = _store(tmp_path)
    try:
        digests = [hashlib.sha256(f"pool-{i}".encode()).hexdigest()
                   for i in range(6)]
        exps = [_experiment(store, digest=d, now=NOW) for d in digests]
        transition(store, exps[0]["experiment_id"], "ablating",
                   actor="system", now=NOW)
        transition(store, exps[1]["experiment_id"], "ablating",
                   actor="system", now=NOW)
        transition(store, exps[1]["experiment_id"], "screened",
                   actor="system", now=NOW)
        _to_shadow(store, exps[2]["experiment_id"])
        _to_shadow(store, exps[3]["experiment_id"])
        # exps[4], exps[5] stay proposed.
        assert pool_counts(store, now=NOW) == {"offline": 4, "shadow": 2}
    finally:
        store.close()


async def test_lab_refuses_new_proposal_when_offline_pool_full(tmp_path):
    store = _store(tmp_path)
    try:
        for i in range(4):
            _experiment(store, digest=hashlib.sha256(
                f"pool-{i}".encode()).hexdigest(), now=NOW)

        class _ExplodingAI:
            available = True

            async def propose_feature_hypothesis(self, **kwargs):
                raise AssertionError(
                    "LLM must not be called while the offline pool is full")

        lab = FeatureHypothesisLab(store, _ExplodingAI(),
                                   venues=("hyperliquid",))
        out = await lab.run_once(now=NOW)
        assert out["status"] == "capacity_full", out
        assert out["offline_open"] == 4
    finally:
        store.close()


def test_screened_candidate_waits_when_shadow_pool_full(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest, now=NOW)
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        trial_report = _trial_report(trials=[_passing_trial(sha=digest)])
        report_sha = hashlib.sha256(trial_report).hexdigest()
        trial_reports = {digest: {"trials": [_passing_trial(sha=digest)],
                                  "report_sha256": report_sha}}
        out = advance_one(store, trial_reports=trial_reports, now=NOW)
        assert out["to"] == "screened", out
        # Fill the shadow pool with four other experiments.
        for i in range(4):
            shadow_exp = _experiment(
                store, digest=hashlib.sha256(f"shadow-{i}".encode()).hexdigest(),
                now=NOW)
            _to_shadow(store, shadow_exp["experiment_id"])
        assert pool_counts(store, now=NOW)["shadow"] == 4
        payload = _candidate_payload(proposal_sha=digest)
        out = advance_one(store, candidates={digest: (payload, True)}, now=NOW)
        assert out == {"status": "nothing_to_advance", "advanced": None}, out
        current = get_experiment(store, exp["experiment_id"])
        assert current["status"] == "screened"
        waits = [e for e in (current["outcome_json"] or [])
                 if e.get("stage") == "shadow-capacity"]
        assert len(waits) == 1
        assert waits[0]["outcome"]["shadow_open"] == 4
        # A second tick with the same state records no duplicate wait.
        out = advance_one(store, candidates={digest: (payload, True)}, now=NOW)
        assert out == {"status": "nothing_to_advance", "advanced": None}, out
        current = get_experiment(store, exp["experiment_id"])
        waits = [e for e in (current["outcome_json"] or [])
                 if e.get("stage") == "shadow-capacity"]
        assert len(waits) == 1
        # Free one shadow slot: the screened candidate advances.
        shadow_exps = [e for e in recent_experiments(store, now=NOW)
                       if e["status"] == "shadow"]
        transition(store, shadow_exps[0]["experiment_id"], "discarded",
                   actor="system",
                   finding={"kind": "negative_result",
                            "summary": "Freeing a shadow slot for the test.",
                            "evidence_ref": "test",
                            "recorded_by": "system"},
                   now=NOW)
        out = advance_one(store, candidates={digest: (payload, True)}, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "shadow", out
    finally:
        store.close()


# --- D17: record every outcome once ----------------------------------------


def test_record_trials_stores_rejection_status_and_reason(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        failing = _passing_trial(sha=digest)
        failing["retrospective_candidate_status"] = "validation_rejected"
        failing["holdout_examined"] = False
        failing.pop("holdout_brier_delta", None)
        saved = record_trials(store, [failing], report_sha256=SHA, now=NOW)
        assert saved == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT outcome_status, outcome_reason
                   FROM rival_llm_hypothesis_trials
                   WHERE proposal_sha256=?""", (digest,)).fetchone()
        assert row["outcome_status"] == "validation_rejected"
        assert row["outcome_reason"] == "purged validation gates not passed"
        # The proof-plan replay: a second save of the same report
        # records zero new rows.
        assert record_trials(store, [failing], report_sha256=SHA,
                             now=NOW) == 0
        # A trial without a screen outcome stores no status.
        other = _passing_trial(sha=digest)
        other["horizon_minutes"] = 15
        other.pop("retrospective_candidate_status", None)
        assert record_trials(store, [other], report_sha256=SHA, now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT outcome_status FROM rival_llm_hypothesis_trials
                   WHERE proposal_sha256=? AND horizon_minutes=15""",
                (digest,)).fetchone()
        assert row["outcome_status"] is None
    finally:
        store.close()


def test_transition_idempotency_key_returns_existing_event(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store, digest=SHA, now=NOW)
        exp_id = exp["experiment_id"]
        out = transition(store, exp_id, "ablating", actor="system",
                         idempotency_key="ablation:abc:def", now=NOW)
        assert out["status"] == "ablating"
        # Creation event + this transition.
        assert len(out["outcome_json"]) == 2
        # The retry returns the experiment as it stands — no second
        # event, no illegal-transition error.
        out2 = transition(store, exp_id, "ablating", actor="system",
                          idempotency_key="ablation:abc:def", now=NOW)
        assert out2["status"] == "ablating"
        assert len(out2["outcome_json"]) == 2
        assert out2["outcome_json"][1]["idempotency_key"] == "ablation:abc:def"
        # A different key for the same step is still illegal.
        with pytest.raises(ValueError, match="illegal experiment transition"):
            transition(store, exp_id, "ablating", actor="system",
                       idempotency_key="ablation:abc:xyz", now=NOW)
        # A transition without a key still appends normally.
        out3 = transition(store, exp_id, "screened", actor="system", now=NOW)
        assert len(out3["outcome_json"]) == 3
    finally:
        store.close()


def test_replayed_trial_report_is_a_noop(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp1 = _experiment(store, digest=digest, now=NOW)
        exp2 = _experiment(store, digest=digest, now=NOW + timedelta(minutes=1))
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        trial_report = _trial_report(trials=[_passing_trial(sha=digest)])
        report_sha = hashlib.sha256(trial_report).hexdigest()
        trial_reports = {digest: {"trials": [_passing_trial(sha=digest)],
                                  "report_sha256": report_sha}}
        out = advance_one(store, trial_reports=trial_reports, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "screened", out
        # The ablation event carries the idempotency key.
        current = get_experiment(store, exp1["experiment_id"])
        ablation = [e for e in current["outcome_json"]
                    if e.get("stage") == "screened"]
        assert len(ablation) == 1
        assert ablation[0]["idempotency_key"] == f"ablation:{digest}:{report_sha}"
        assert is_report_consumed(store, digest, report_sha) is True
        # Re-delivering the same report advances nothing: the second
        # experiment waits instead of re-applying the same trials.
        out = advance_one(store, trial_reports=trial_reports, now=NOW)
        assert out == {"status": "nothing_to_advance", "advanced": None}, out
        assert get_experiment(store, exp2["experiment_id"])["status"] == "ablating"
        # Creation event + verify event only: the re-delivered report
        # appended nothing.
        assert len(get_experiment(store, exp2["experiment_id"])
                   ["outcome_json"]) == 2
        # The consumed key itself is apply-once.
        assert record_consumed_report(store, digest, report_sha, now=NOW) is False
    finally:
        store.close()


def test_blocked_receipt_recorded_once_and_closes_experiment(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest, now=NOW)
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        assert record_blocked_receipt(
            store, venue="hyperliquid", asset=None, requested_months=6,
            missing_months=["2026-02", "2026-01"], report_sha256=SHA,
            now=NOW) == 1
        # Re-delivery records nothing new.
        assert record_blocked_receipt(
            store, venue="hyperliquid", asset=None, requested_months=6,
            missing_months=["2026-02", "2026-01"], report_sha256=SHA,
            now=NOW) == 0
        receipts = list_blocked_receipts(store)
        assert len(receipts) == 1
        assert receipts[0]["missing_months"] == ["2026-01", "2026-02"]
        # Every design venue is blocked: the driver closes the
        # experiment with a finding naming the missing months.
        out = advance_one(store, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "discarded", out
        assert get_experiment(store, exp["experiment_id"])["status"] == "discarded"
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert "2026-01" in findings[0]["summary"]
        assert "2026-02" in findings[0]["summary"]
    finally:
        store.close()


def test_single_blocked_venue_does_not_close_experiment(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest, now=NOW,
                          venues=("hyperliquid", "okx"))
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        # Only one of the two design venues is blocked: another venue's
        # run may still ablate this proposal, so it waits.
        assert record_blocked_receipt(
            store, venue="hyperliquid", asset=None, requested_months=6,
            missing_months=["2026-01"], report_sha256=SHA, now=NOW) == 1
        out = advance_one(store, now=NOW)
        assert out == {"status": "nothing_to_advance", "advanced": None}, out
        assert get_experiment(store, exp["experiment_id"])["status"] == "ablating"
    finally:
        store.close()


def test_blocked_report_tool_parses_receipt_and_skips_without_discloud(
        tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from discloud_hypothesis_results import blocked_receipt, main

    report = {
        "schema_version": "rival-native-source-research-v1",
        "status": "source_months_incomplete", "venue": "okx", "asset": None,
        "requested_months": ["2026-01", "2026-02", "2026-03"],
        "source_scopes": [
            {"contract": "BTC", "quote": "USDT", "interval": 5,
             "verified_full_months": 1,
             "missing_or_incomplete_months": ["2026-02", "2026-03"]},
            {"contract": "ETH", "quote": "USDT", "interval": 5,
             "verified_full_months": 2,
             "missing_or_incomplete_months": ["2026-01"]},
        ],
    }
    path = tmp_path / "source-okx-research.json"
    path.write_text(json.dumps(report))
    receipt = blocked_receipt(path)
    assert receipt == {"venue": "okx", "asset": None, "requested_months": 3,
                       "missing_months": ["2026-01", "2026-02", "2026-03"]}
    # A trial report is not a blocked report.
    trial_path = tmp_path / "source-hyperliquid-research.json"
    trial_path.write_text(json.dumps({
        "schema_version": "rival-native-source-research-v1",
        "venue": "hyperliquid",
        "llm_hypothesis_research": {"experiments": []}}))
    assert blocked_receipt(trial_path) is None
    # Without Discloud credentials the tool reports and exits 0.
    monkeypatch.delenv("DISCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("DISCLOUD_APP_ID", raising=False)
    assert main(["--reports-dir", str(tmp_path)]) == 0


# --- D18: card progress + blockers ------------------------------------------


def test_status_card_surfaces_archive_v1_receipt(tmp_path):
    store = _store(tmp_path)
    try:
        _seed_receipt(store, family="archive_v1", recorded_at=NOW)
        keys = [key for key, _ in research_alerts(store, now=NOW)]
        assert "archive-v1:promoted:hyperliquid:BTC:5m" in keys
    finally:
        store.close()


def test_status_card_shows_shadow_progress(tmp_path):
    store = _store(tmp_path)
    try:
        _seed_promotion_row(store, family="archive_v1", targets=62, days=8,
                            qualified=False, evaluated_at=NOW - timedelta(hours=1))
        _seed_promotion_row(store, family="liq_v1", targets=180, days=22,
                            qualified=True, evaluated_at=NOW - timedelta(hours=1),
                            horizon_minutes=15)
        alerts = dict(research_alerts(store, now=NOW))
        assert alerts["shadow-progress:archive_v1:BTC:5m"] == (
            "📊 archive_v1 BTC 5m: 8/21 days · 62/150 windows")
        # A qualified family shows no progress line — its promotion
        # receipt already announces it.
        assert "shadow-progress:liq_v1:BTC:15m" not in alerts
    finally:
        store.close()


def test_status_card_shows_blocker(tmp_path):
    store = _store(tmp_path)
    try:
        assert record_blocked_receipt(
            store, venue="okx", asset=None, requested_months=6,
            missing_months=["2026-01", "2026-02"], report_sha256=SHA,
            now=NOW) == 1
        alerts = dict(research_alerts(store, now=NOW))
        assert alerts["research-blocked:okx"] == (
            "⛔ Research blocked at okx: 2 month(s) missing (2026-01, 2026-02)")
    finally:
        store.close()
