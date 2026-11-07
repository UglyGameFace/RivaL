"""Autonomous loop Phase 1 — driver tests (spec §6).

Tests cover the driver's wiring, not the numeric gates: receipt
verification uses the real verify_proposals() via a seeded hypotheses
row, ablation/screen/shadow use the real loop stage functions with
trial reports and candidate artifacts supplied through the driver's
own Drive-handoff inputs, and promotion uses a stub at the
FamilyPromotion.selected() boundary. The numeric gates themselves are
covered by test_llm_hypothesis_pipeline.py,
test_research_candidate_registry.py, and
test_batch2_phase3_research_loop.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.perpetuals.model_checkpoints import (
    FEATURE_CONTRACT,
    TARGET_CONTRACT,
)
from parlay_bot.perpetuals.research_hypotheses import MODEL, FeatureHypothesisLab
from parlay_bot.perpetuals.research_loop import (
    create_experiment,
    list_findings,
    transition,
)
from parlay_bot.research.research_loop_driver import (
    advance_one,
    drive_research_loop_tick,
    fetch_loop_inputs,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SHA = "a" * 64
OTHER_SHA = "b" * 64
ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path):
    return SQLiteHotStore(tmp_path / "loop.sqlite")


def _proposal_digest(*, slot: datetime, hypothesis: str = "volume_only") -> str:
    identity = {
        "asset": "BTC", "slot_utc": slot.isoformat(), "model": MODEL,
        "evidence_sha256": SHA, "hypothesis": hypothesis,
        "venues": ["hyperliquid"],
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _seed_proposal(store, *, digest: str, slot: datetime) -> None:
    """Write the hypotheses-table row the lab would have stored."""
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


def _experiment(store, *, digest: str, now: datetime = NOW):
    return create_experiment(
        store, proposer="llm", hypothesis="volume_only",
        design={"asset": "BTC", "venues": ["hyperliquid"],
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


def _family_for(payload: bytes) -> str:
    model_sha256 = json.loads(payload)["model_sha256"]
    return f"historical_{model_sha256[:48]}"


class _Promoter:
    """Stub at the FamilyPromotion.selected() boundary."""

    def __init__(self, selected):
        self._selected = selected

    def selected(self, **kwargs):
        return self._selected


class _FakeDrive:
    """Stub at the Drive read boundary: canned pages per folder."""

    def __init__(self, *, models=None, reports=None):
        self.models = models or []
        self.reports = reports or []

    async def read_verified_research_candidates(
            self, *, limit=20, page_token=None, return_next_page=False):
        return (self.models, None) if return_next_page else self.models

    async def read_verified_research_reports(
            self, *, limit=20, page_token=None, return_next_page=False):
        return (self.reports, None) if return_next_page else self.reports


def _seed_promotion_row(store, *, family: str, selected: str | None,
                        evaluated_at: datetime, status: str = "promoted_paper"):
    from parlay_bot.perpetuals.family_governance import FamilyPromotion

    FamilyPromotion(store).initialize()
    evidence = {family: {"qualified": selected == family, "targets": 180,
                         "days": 22, "challenger_trades": 61,
                         "quote_coverage": 0.87, "ece": 0.06,
                         "forward_gain": 0.009}}
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue, contract, quote, asset, horizon_minutes, evaluated_at,
                selected_family, status, reason, evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5, evaluated_at.isoformat(),
             selected, status, "test", json.dumps(evidence)),
        )


def _drive_to_shadow(store, *, digest: str, now: datetime = NOW):
    """Advance one experiment proposed -> ablating -> screened -> shadow."""
    trial_report = _trial_report(trials=[_passing_trial(sha=digest)])
    report_sha = hashlib.sha256(trial_report).hexdigest()
    trial_reports = {digest: {"trials": [_passing_trial(sha=digest)],
                              "report_sha256": report_sha}}
    payload = _candidate_payload(proposal_sha=digest)
    out = advance_one(store, now=now)
    assert out["status"] == "advanced" and out["to"] == "ablating", out
    out = advance_one(store, trial_reports=trial_reports, now=now)
    assert out["status"] == "advanced" and out["to"] == "screened", out
    out = advance_one(store, candidates={digest: (payload, True)}, now=now)
    assert out["status"] == "advanced" and out["to"] == "shadow", out
    return payload


def test_driver_advances_full_chain_to_discarded(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest)
        _drive_to_shadow(store, digest=digest)
        # No promotion row exists and the 30-day lookback expires:
        # discarded with a finding, never silently.
        out = advance_one(store, now=NOW + timedelta(days=31))
        assert out["status"] == "advanced" and out["to"] == "discarded", out
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
        assert findings[0]["recorded_by"] == "system"
    finally:
        store.close()


def test_driver_advances_full_chain_to_promoted(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest)
        payload = _drive_to_shadow(store, digest=digest)
        family = _family_for(payload)
        _seed_promotion_row(store, family=family, selected=family,
                            evaluated_at=NOW + timedelta(hours=1))
        out = advance_one(store, promoter=_Promoter(family),
                          now=NOW + timedelta(hours=1))
        assert out["status"] == "advanced" and out["to"] == "promoted", out
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "discovery"
        assert findings[0]["recorded_by"] == "system"
    finally:
        store.close()


def test_shadow_observation_does_not_spam_without_new_evaluation(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        _experiment(store, digest=digest)
        payload = _drive_to_shadow(store, digest=digest)
        family = _family_for(payload)
        # An evaluation exists but selects nobody: one observation is
        # recorded, then the next tick has nothing new and does nothing.
        _seed_promotion_row(store, family=family, selected=None,
                            evaluated_at=NOW + timedelta(hours=1),
                            status="held")
        out = advance_one(store, promoter=_Promoter(None),
                          now=NOW + timedelta(hours=1))
        assert out["status"] == "advanced" and out["to"] == "shadow", out
        out = advance_one(store, promoter=_Promoter(None),
                          now=NOW + timedelta(hours=2))
        assert out["status"] == "nothing_to_advance", out
    finally:
        store.close()


def test_driver_tick_with_nothing_to_advance_does_nothing(tmp_path):
    store = _store(tmp_path)
    try:
        assert advance_one(store, now=NOW)["status"] == "nothing_to_advance"
        # A terminal experiment is not revisited either.
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        _experiment(store, digest=digest)
        _drive_to_shadow(store, digest=digest)
        advance_one(store, now=NOW + timedelta(days=31))
        assert advance_one(store, now=NOW + timedelta(days=32))["status"] \
            == "nothing_to_advance"
    finally:
        store.close()


def test_driver_skips_tick_on_memory_pressure(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest)
        out = advance_one(store, memory_pressure=True, now=NOW)
        assert out["status"] == "skipped_memory_pressure"
        from parlay_bot.perpetuals.research_loop import get_experiment

        assert get_experiment(store, exp["experiment_id"])["status"] == "proposed"
    finally:
        store.close()


def test_mismatched_trial_sha_is_ignored(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest)
        advance_one(store, now=NOW)
        # A trial report naming a different proposal must not advance
        # this experiment; it stays ablating.
        trial_report = _trial_report(trials=[_passing_trial(sha=OTHER_SHA)])
        report_sha = hashlib.sha256(trial_report).hexdigest()
        trial_reports = {OTHER_SHA: {"trials": [_passing_trial(sha=OTHER_SHA)],
                                     "report_sha256": report_sha}}
        out = advance_one(store, trial_reports=trial_reports, now=NOW)
        assert out["status"] == "nothing_to_advance", out
        from parlay_bot.perpetuals.research_loop import get_experiment

        assert get_experiment(store, exp["experiment_id"])["status"] == "ablating"
    finally:
        store.close()


def test_actor_llm_transition_still_refused(tmp_path):
    store = _store(tmp_path)
    try:
        exp = _experiment(store, digest=_proposal_digest(
            slot=NOW - timedelta(hours=1)))
        with pytest.raises(ValueError, match="llm"):
            transition(store, exp["experiment_id"], "ablating", actor="llm")
    finally:
        store.close()


def test_driver_spends_no_llm_calls_by_construction():
    """The driver's entry points take no AI client and no ai argument."""
    for fn in (advance_one, drive_research_loop_tick, fetch_loop_inputs):
        assert "ai" not in inspect.signature(fn).parameters


@pytest.mark.asyncio
async def test_fetch_loop_inputs_ignores_mismatched_sha():
    drive = _FakeDrive(
        models=[("f1", _candidate_payload(proposal_sha=OTHER_SHA))],
        reports=[("f2", _trial_report(trials=[_passing_trial(sha=OTHER_SHA)]))],
    )
    trial_reports, candidates = await fetch_loop_inputs(drive)
    assert SHA not in trial_reports
    assert SHA not in candidates
    assert OTHER_SHA in trial_reports
    assert OTHER_SHA in candidates
    assert candidates[OTHER_SHA][1] is True  # models folder = eligible


@pytest.mark.asyncio
async def test_fetch_loop_inputs_rejected_candidate_is_ineligible():
    drive = _FakeDrive(
        reports=[("f2", _candidate_payload(proposal_sha=SHA))],
    )
    _trial_reports, candidates = await fetch_loop_inputs(drive)
    assert candidates[SHA][1] is False  # reports folder = screen failed


@pytest.mark.asyncio
async def test_async_tick_drives_the_full_chain(tmp_path):
    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        _experiment(store, digest=digest)
        drive = _FakeDrive(
            models=[("f1", _candidate_payload(proposal_sha=digest))],
            reports=[("f2", _trial_report(trials=[_passing_trial(sha=digest)]))],
        )
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "ablating", out
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "screened", out
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "shadow", out
        # No evaluation row yet: the shadow tick waits, it does not invent
        # an outcome — but the tick's one action is then opening the
        # next scheduled variant (Phase 3: advance OR open per tick).
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "opened", out
        assert out["variant_id"] == "archive-premium-off", out
    finally:
        store.close()


@pytest.mark.asyncio
async def test_async_tick_skips_on_drive_failure(tmp_path):
    class _FailingDrive:
        async def read_verified_research_candidates(self, **kwargs):
            raise RuntimeError("Drive down")

        async def read_verified_research_reports(self, **kwargs):
            raise RuntimeError("Drive down")

    store = _store(tmp_path)
    try:
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest)
        # The verify stage needs no Drive: it still advances.
        out = await drive_research_loop_tick(store, drive=_FailingDrive(), now=NOW)
        assert out["status"] == "advanced" and out["to"] == "ablating", out
        # The ablation stage needs Drive: a Drive failure skips the tick.
        out = await drive_research_loop_tick(store, drive=_FailingDrive(), now=NOW)
        assert out["status"] == "drive_unavailable", out
        from parlay_bot.perpetuals.research_loop import get_experiment

        assert get_experiment(store, exp["experiment_id"])["status"] == "ablating"
    finally:
        store.close()


def test_status_card_counts_line(tmp_path):
    from parlay_bot.perpetuals.research_loop import ResearchLoop

    store = _store(tmp_path)
    try:
        # No experiments: no counts line, no noise.
        assert not [key for key, _ in research_alerts(store, now=NOW)
                    if key == "research-loop:counts"]
        digest = _proposal_digest(slot=NOW - timedelta(hours=1))
        _seed_proposal(store, digest=digest, slot=NOW - timedelta(hours=1))
        exp = _experiment(store, digest=digest)
        loop = ResearchLoop(store)
        loop.verify(exp["experiment_id"],
                    {"status": "verified",
                     "source": "live_postgresql_llm_hypotheses",
                     "proposals": [{
                         "asset": "BTC",
                         "slot_utc": (NOW - timedelta(hours=1)).isoformat(),
                         "model": MODEL, "evidence_sha256": SHA,
                         "hypothesis": "volume_only", "venues": ["hyperliquid"],
                         "record_sha256": digest}]},
                    venue="hyperliquid", now=NOW)
        loop.ablate(exp["experiment_id"], trials=[_passing_trial(sha=digest)],
                    report_sha256=SHA, now=NOW)
        # One open experiment, none closed: the line reports the open count.
        counts = [text for key, text in research_alerts(store, now=NOW)
                  if key == "research-loop:counts"]
        assert len(counts) == 1
        assert "1 open experiment" in counts[0]
        assert "0 promoted, 0 discarded, 0 failed" in counts[0]
    finally:
        store.close()


def _bare_bot(*, research_enabled: bool):
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.bot import RivalDiscordClient

    bot = object.__new__(RivalDiscordClient)
    bot.settings = Settings(
        _env_file=None, rival_llm_background_opt_in=False,
        rival_research_loop_enabled=research_enabled,
    )
    return bot


@pytest.mark.asyncio
async def test_driver_loop_does_not_run_when_disabled():
    bot = _bare_bot(research_enabled=False)
    waited = []

    async def fake_wait():
        waited.append(True)

    bot.wait_until_ready = fake_wait
    await bot._research_loop_driver()
    assert waited == []  # exited at the opt-in head


@pytest.mark.asyncio
async def test_driver_loop_proceeds_with_llm_opted_out():
    bot = _bare_bot(research_enabled=True)
    waited = []

    async def fake_wait():
        waited.append(True)
        raise asyncio.CancelledError

    bot.wait_until_ready = fake_wait
    with pytest.raises(asyncio.CancelledError):
        await bot._research_loop_driver()
    assert waited == [True]


def test_driver_wiring_requires_research_enabled():
    """Task creation, loop head, and watchdog all check the gate."""
    code = (ROOT / "src/parlay_bot/discord_app/bot.py").read_text()
    assert "_research_loop_task" in code
    assert "self._research_loop_driver(), name=\"rival-research-loop-driver\"" in code
    assert "(\"_research_loop_task\", self._research_loop_driver," in code
    assert "\"_research_loop_task\", \"_candidate_task\"" in code
    assert 'attr == "_research_loop_task" and not self.settings.rival_research_loop_enabled' in code
    assert "drive=self.research_loop_drive" in code
