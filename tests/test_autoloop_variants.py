"""Autonomous loop Phase 3 — scheduled variant grid tests (spec §6, D9/D10/D13).

Tests cover the driver's scheduled path, not the numeric gates: the
grid enumerates deterministically, a scheduled variant advances
through every stage via the driver and closes with a finding
(promoted AND discarded), caps are enforced (≤2 new scheduled
variants/week, offline pool full refuses opening), LLM-mask variants
follow the existing verify/ablate path, and no new family can be
created by the loop. The numeric gates themselves are covered by
test_archive_model.py, test_gbm_model.py, and
test_batch2_phase3_research_loop.py.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.research_hypotheses import MODEL, validate_proposal
from parlay_bot.perpetuals.research_loop import (
    create_experiment,
    get_experiment,
    list_findings,
)
from parlay_bot.research import archive_training, gbm_training, volcal_training
from parlay_bot.research.archive_features import (
    FEATURE_CONTRACT,
    FEATURE_DIGEST,
    FEATURES,
    GROUPS,
)
from parlay_bot.research.pooled_training import PARAMS as POOLED_PARAMS
from parlay_bot.research.research_loop_driver import (
    _candidate_from_payload,
    advance_one,
    drive_research_loop_tick,
    fetch_loop_inputs,
)
from parlay_bot.research.scheduled_variants import (
    SCHEDULED_HYPOTHESIS,
    SCHEDULED_VARIANTS,
    design_sha256,
    lookup_variant,
    open_scheduled_variant,
    scheduled_request,
    scheduled_shadow_identity,
    screen_scheduled_artifact,
)
from parlay_bot.research.volcal_features import (
    FEATURE_DIGEST as VOLCAL_FEATURE_DIGEST,
)
from parlay_bot.research.volcal_features import (
    FEATURES as VOLCAL_FEATURES,
)
from parlay_bot.research.volcal_features import (
    GROUPS as VOLCAL_GROUPS,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
PROV = {
    "provider_sha256_by_month": {"binance:2025-01": "a" * 64},
    "months_without_provider_hash": [],
}


def _store(tmp_path):
    return SQLiteHotStore(tmp_path / "loop.sqlite")


def _archive_doc(design_sha, *, asset: str = "BTC"):
    """A structurally valid archive artifact doc for a scheduled design."""
    doc = {
        "schema": archive_training.SCHEMA,
        "family": archive_training.FAMILY,
        "model_version": archive_training.MODEL_VERSION,
        "asset": asset,
        "horizon_minutes": 5,
        "feature_contract": FEATURE_CONTRACT,
        "features": list(FEATURES),
        "feature_digest": FEATURE_DIGEST,
        "weights": [0.01] * (len(FEATURES) + 1),
        "calibration": {"status": "evaluated", "a": 1.0, "b": 0.0},
        "provenance": dict(PROV),
        "gates_passed": True,
        "trained_through": "2025-03-01T23:55:00+00:00",
        "evaluation_status": "held-out evaluated",
        "scheduled_design_sha256": design_sha,
    }
    doc["weights_sha256"] = archive_training.artifact_identity(doc)[
        "weights_sha256"]
    return doc


def _gbm_doc(design_sha, *, asset: str = "ETH"):
    """A structurally valid GBM artifact doc for a scheduled design."""
    trees = [[[0, 0.5, 1, 0, 1, 2], [0.1], [-0.1]]]  # one tree, three nodes
    return {
        "schema": gbm_training.SCHEMA,
        "family": gbm_training.FAMILY,
        "model_version": gbm_training.MODEL_VERSION,
        "asset": asset,
        "horizon_minutes": 5,
        "feature_contract": FEATURE_CONTRACT,
        "features": list(FEATURES),
        "feature_digest": FEATURE_DIGEST,
        "trees": trees,
        "weights_sha256": hashlib.sha256(
            json.dumps(trees, separators=(",", ":")).encode()).hexdigest(),
        "params": dict(SCHEDULED_VARIANTS[1]["gbm_params"]),
        "calibration": {"status": "evaluated", "a": 1.0, "b": 0.0},
        "provenance": dict(PROV),
        "gates_passed": True,
        "trained_through": "2025-03-01T23:55:00+00:00",
        "evaluation_status": "held-out evaluated",
        "scheduled_design_sha256": design_sha,
    }


def _volcal_doc(design_sha, *, asset: str = "BTC", groups_off=()):
    """A structurally valid volcal artifact doc for a scheduled design."""
    doc = {
        "schema": volcal_training.SCHEMA,
        "family": volcal_training.FAMILY,
        "model_version": volcal_training.MODEL_VERSION,
        "asset": asset,
        "horizon_minutes": 5,
        "feature_contract": "volcal-candle-vol-v1",
        "features": list(VOLCAL_FEATURES),
        "feature_digest": VOLCAL_FEATURE_DIGEST,
        "weights": [0.01] * (len(VOLCAL_FEATURES) + 1),
        "standardizer": {"mean": [0.0] * len(VOLCAL_FEATURES),
                         "std": [1.0] * len(VOLCAL_FEATURES)},
        "calibration": {"status": "evaluated", "a": 1.0, "b": 0.0},
        "provenance": dict(PROV),
        "gates_passed": True,
        "trained_through": "2025-03-01T23:55:00+00:00",
        "training_rows": 1000,
        "evaluation_status": "held-out evaluated",
        "scheduled_design_sha256": design_sha,
    }
    if groups_off:
        doc["feature_groups_off"] = list(groups_off)
    doc["weights_sha256"] = volcal_training.artifact_identity(doc)[
        "weights_sha256"]
    return doc


def _trial_indices(design: dict) -> list[int]:
    """Feature indices the variant keeps: all features minus its groups off."""
    if design.get("model_shape") == "volcal-offline-v1":
        groups = VOLCAL_GROUPS
        total = len(VOLCAL_FEATURES)
    else:
        groups = GROUPS
        total = len(FEATURES)
    off = set()
    for group in design.get("feature_groups_off") or []:
        off.update(groups[group])
    return [i for i in range(total) if i not in off]


def _passing_trial(design_sha, *, design, asset: str = "BTC"):
    return {"asset": asset, "venue": "hyperliquid", "contract": asset,
            "quote": "USDC", "horizon_minutes": 5,
            "hypothesis": SCHEDULED_HYPOTHESIS,
            "feature_indices": _trial_indices(design),
            "proposal_sha256": design_sha,
            "validation_count": 200,
            "baseline_validation_brier": 0.25,
            "llm_mask_validation_brier": 0.238,
            "validation_brier_delta": 0.012, "holdout_brier_delta": 0.011,
            "holdout_examined": True,
            "retrospective_candidate_status": "screened_for_future_shadow"}


def _failing_trial(design_sha, *, design, asset: str = "BTC"):
    trial = _passing_trial(design_sha, design=design, asset=asset)
    trial["holdout_brier_delta"] = 0.0
    return trial


class _Promoter:
    """Stub at the FamilyPromotion.selected() boundary."""

    def __init__(self, selected):
        self._selected = selected

    def selected(self, **kwargs):
        return self._selected


class _FakeDrive:
    """Immutable files at the verified Drive read and write boundaries."""

    def __init__(self):
        self.files = {"model": {}, "report": {}}

    async def ensure_research_output(self, *, artifact, batch_id, kind):
        self.files[kind][artifact.file_name] = artifact.path.read_bytes()

    def _list(self, kind, limit, name_prefix, return_next_page):
        matches = [(name, content) for name, content in self.files[kind].items()
                   if name_prefix is None or name.startswith(name_prefix)][:limit]
        return (matches, None) if return_next_page else matches

    async def read_verified_research_candidates(
            self, *, limit=20, page_token=None, return_next_page=False, name_prefix=None):
        return self._list("model", limit, name_prefix, return_next_page)

    async def read_verified_research_reports(
            self, *, limit=20, page_token=None, return_next_page=False, name_prefix=None):
        return self._list("report", limit, name_prefix, return_next_page)

    async def aclose(self):
        pass


def _seed_promotion_row(store, *, family: str, asset: str, selected: str,
                        evaluated_at: datetime, weights_sha: str):
    from parlay_bot.perpetuals.family_governance import FamilyPromotion

    FamilyPromotion(store).initialize()
    evidence = {family: {"qualified": selected == family, "targets": 180,
                         "days": 22, "challenger_trades": 61,
                         "quote_coverage": 0.87, "ece": 0.06,
                         "forward_gain": 0.009,
                         "artifact": {
                             "feature_digest": FEATURE_DIGEST,
                             "weights_sha256": weights_sha,
                             "trained_through": "2025-03-01T23:55:00+00:00",
                             "graded_count": 180}}}
    with store.connect() as conn:
        # One row per scope (unique on venue/contract/quote/horizon):
        # a newer evaluation replaces the stored row.
        conn.execute(
            """DELETE FROM rival_family_promotion
               WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?""",
            ("hyperliquid", asset, "USDC", 5),
        )
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue, contract, quote, asset, horizon_minutes, evaluated_at,
                selected_family, status, reason, evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", asset, "USDC", asset, 5,
             evaluated_at.isoformat(), selected, "promoted_paper", "test",
             json.dumps(evidence)),
        )


def _drive_scheduled_to_shadow(store, *, design_sha, trial,
                               artifact_payload, now=NOW):
    """Advance one scheduled experiment proposed -> ablating -> screened -> shadow."""
    report_sha = hashlib.sha256(
        json.dumps(trial, sort_keys=True).encode()).hexdigest()
    trial_reports = {design_sha: {"trials": [trial],
                                  "report_sha256": report_sha}}
    out = advance_one(store, now=now)
    assert out["status"] == "advanced" and out["to"] == "ablating", out
    out = advance_one(store, trial_reports=trial_reports, now=now)
    assert out["status"] == "advanced" and out["to"] == "screened", out
    out = advance_one(store, candidates={design_sha: (artifact_payload, True)},
                      now=now)
    assert out["status"] == "advanced" and out["to"] == "shadow", out


def test_grid_enumerates_deterministically():
    assert [d["variant_id"] for d in SCHEDULED_VARIANTS] == [
        "archive-premium-off", "gbm-trees-150", "archive-tape-off",
        "archive-window-per-venue",
        "volcal-full", "volcal-no-vol", "volcal-no-compression",
        "volcal-no-volume", "volcal-no-dc",
    ]
    hashes = [design_sha256(d) for d in SCHEDULED_VARIANTS]
    assert len(set(hashes)) == 9
    assert hashes == [design_sha256(d) for d in SCHEDULED_VARIANTS]
    # The design hash is the proposal SHA the loop joins on.
    for design in SCHEDULED_VARIANTS:
        assert lookup_variant(design_sha256(design)) is design
    assert lookup_variant("f" * 64) is None
    # GBM params are a valid override of the pooled PARAMS keys.
    assert set(SCHEDULED_VARIANTS[1]["gbm_params"]) <= set(POOLED_PARAMS)
    # Every groups-off name is a real feature group for its own model
    # shape: archive groups for archive designs, volcal groups for
    # volcal designs (the base candle group is never ablatable).
    for design in SCHEDULED_VARIANTS:
        if design["model_shape"] == "volcal-offline-v1":
            for group in design["feature_groups_off"]:
                assert group in VOLCAL_GROUPS and group != "candle"
        else:
            for group in design["feature_groups_off"]:
                assert group in GROUPS


def test_open_scheduled_variant_opens_grid_in_order_and_caps_weekly(tmp_path):
    store = _store(tmp_path)
    try:
        first = open_scheduled_variant(store, now=NOW)
        assert first is not None
        assert first["proposer"] == "scheduled"
        assert first["hypothesis"] == SCHEDULED_HYPOTHESIS
        assert first["design_json"]["variant_id"] == "archive-premium-off"
        assert first["proposal_sha256"] == design_sha256(SCHEDULED_VARIANTS[0])
        assert first["status"] == "proposed"
        second = open_scheduled_variant(store, now=NOW)
        assert second["design_json"]["variant_id"] == "gbm-trees-150"
        # Weekly cap: two new scheduled variants this week is the limit.
        assert open_scheduled_variant(store, now=NOW) is None
        # A week later the budget resets and the grid continues.
        third = open_scheduled_variant(store, now=NOW + timedelta(days=8))
        assert third["design_json"]["variant_id"] == "archive-tape-off"
    finally:
        store.close()


def test_open_refuses_when_offline_pool_full(tmp_path):
    store = _store(tmp_path)
    try:
        for i in range(4):
            create_experiment(
                store, proposer="llm", hypothesis="volume_only",
                design={"asset": "BTC", "venues": ["hyperliquid"]},
                proposal_sha256=hashlib.sha256(f"llm-{i}".encode()).hexdigest(),
                now=NOW,
            )
        assert open_scheduled_variant(store, now=NOW) is None
    finally:
        store.close()


def test_open_permanently_skips_design_after_two_ablation_failures(tmp_path):
    store = _store(tmp_path)
    try:
        design = SCHEDULED_VARIANTS[0]
        sha = design_sha256(design)
        for i in range(2):
            create_experiment(
                store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
                design=copy.deepcopy(design), proposal_sha256=sha, now=NOW,
            )
            out = advance_one(store, now=NOW)
            assert out["to"] == "ablating", out
            report_sha = hashlib.sha256(f"report-{i}".encode()).hexdigest()
            out = advance_one(
                store,
                trial_reports={sha: {
                    "trials": [_failing_trial(sha, design=design)],
                    "report_sha256": report_sha}},
                now=NOW,
            )
            assert out["to"] == "discarded", out
        # 100 days later: past the 90-day retry window, but the design
        # failed ablation twice, so it is permanently skipped.
        opened = open_scheduled_variant(store, now=NOW + timedelta(days=100))
        assert opened is not None
        assert opened["design_json"]["variant_id"] == "gbm-trees-150"
    finally:
        store.close()


def test_open_reproposes_design_after_one_ablation_failure(tmp_path):
    store = _store(tmp_path)
    try:
        design = SCHEDULED_VARIANTS[0]
        sha = design_sha256(design)
        create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=copy.deepcopy(design), proposal_sha256=sha, now=NOW,
        )
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        report_sha = hashlib.sha256(b"report-0").hexdigest()
        out = advance_one(
            store,
            trial_reports={sha: {
                "trials": [_failing_trial(sha, design=design)],
                "report_sha256": report_sha}},
            now=NOW,
        )
        assert out["to"] == "discarded", out
        # One failure is not a retirement: past the 90-day window the
        # design may be tried again.
        opened = open_scheduled_variant(store, now=NOW + timedelta(days=100))
        assert opened is not None
        assert opened["design_json"]["variant_id"] == "archive-premium-off"
    finally:
        store.close()


def test_scheduled_verify_fails_unregistered_design(tmp_path):
    store = _store(tmp_path)
    try:
        design = {"variant_id": "not-a-grid-entry", "asset": "BTC",
                  "venues": ["binance"], "model_shape": "archive-offline-v1",
                  "feature_groups_off": ["premium"],
                  "train_window": "common-2021", "gbm_params": None,
                  "calibration_method": "platt-scaling-ece<=0.10",
                  "shadow_mode": "artifact",
                  "scope": {"venue": "hyperliquid", "contract": "BTC",
                            "quote": "USDC", "horizon_minutes": 5}}
        sha = design_sha256(design)
        exp = create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=design, proposal_sha256=sha, now=NOW,
        )
        out = advance_one(store, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "failed", out
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
    finally:
        store.close()


def test_scheduled_variant_closes_discarded_end_to_end(tmp_path):
    store = _store(tmp_path)
    try:
        opened = open_scheduled_variant(store, now=NOW)
        assert opened["design_json"]["variant_id"] == "archive-premium-off"
        sha = opened["proposal_sha256"]
        design = SCHEDULED_VARIANTS[0]
        doc = _archive_doc(sha)
        _drive_scheduled_to_shadow(
            store, design_sha=sha,
            trial=_passing_trial(sha, design=design),
            artifact_payload=json.dumps(doc).encode())
        # No promotion row exists and the 30-day lookback expires:
        # discarded with a finding, never silently.
        out = advance_one(store, now=NOW + timedelta(days=31))
        assert out["status"] == "advanced" and out["to"] == "discarded", out
        findings = list_findings(store, opened["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
        assert findings[0]["recorded_by"] == "system"
    finally:
        store.close()


def test_scheduled_variant_closes_promoted_end_to_end(tmp_path):
    store = _store(tmp_path)
    try:
        opened = open_scheduled_variant(store, now=NOW)
        sha = opened["proposal_sha256"]
        design = SCHEDULED_VARIANTS[0]
        doc = _archive_doc(sha)
        _drive_scheduled_to_shadow(
            store, design_sha=sha,
            trial=_passing_trial(sha, design=design),
            artifact_payload=json.dumps(doc).encode())
        weights_sha = archive_training.artifact_identity(doc)["weights_sha256"]
        _seed_promotion_row(
            store, family="archive_v1", asset="BTC", selected="archive_v1",
            evaluated_at=NOW + timedelta(hours=1), weights_sha=weights_sha)
        out = advance_one(store, promoter=_Promoter("archive_v1"),
                          now=NOW + timedelta(hours=1))
        assert out["status"] == "advanced" and out["to"] == "promoted", out
        findings = list_findings(store, opened["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "discovery"
    finally:
        store.close()


def test_scheduled_variant_waits_when_pinned_fit_differs(tmp_path):
    store = _store(tmp_path)
    try:
        opened = open_scheduled_variant(store, now=NOW)
        sha = opened["proposal_sha256"]
        design = SCHEDULED_VARIANTS[0]
        doc = _archive_doc(sha)
        _drive_scheduled_to_shadow(
            store, design_sha=sha,
            trial=_passing_trial(sha, design=design),
            artifact_payload=json.dumps(doc).encode())
        weights_sha = archive_training.artifact_identity(doc)["weights_sha256"]
        # The evaluation pins a different fit: the variant waits queued.
        _seed_promotion_row(
            store, family="archive_v1", asset="BTC", selected="archive_v1",
            evaluated_at=NOW + timedelta(hours=1), weights_sha="0" * 64)
        out = advance_one(store, promoter=_Promoter("archive_v1"),
                          now=NOW + timedelta(hours=1))
        assert out["status"] == "nothing_to_advance", out
        current = get_experiment(store, opened["experiment_id"])
        assert current["status"] == "shadow"
        queued = [e for e in current["outcome_json"]
                  if (e.get("outcome") or {}).get("queued")]
        assert len(queued) == 1
        # A newer evaluation pinning the variant's own fit unblocks it.
        _seed_promotion_row(
            store, family="archive_v1", asset="BTC", selected="archive_v1",
            evaluated_at=NOW + timedelta(hours=2), weights_sha=weights_sha)
        out = advance_one(store, promoter=_Promoter("archive_v1"),
                          now=NOW + timedelta(hours=2))
        assert out["status"] == "advanced" and out["to"] == "promoted", out
    finally:
        store.close()


def test_new_family_variant_holds_at_screened(tmp_path):
    store = _store(tmp_path)
    try:
        design = SCHEDULED_VARIANTS[1]  # gbm-trees-150
        sha = design_sha256(design)
        exp = create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=copy.deepcopy(design), proposal_sha256=sha, now=NOW,
        )
        doc = _gbm_doc(sha, asset="ETH")
        payload = json.dumps(doc).encode()
        trial = _passing_trial(sha, design=design, asset="ETH")
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        report_sha = hashlib.sha256(b"gbm-report").hexdigest()
        out = advance_one(
            store,
            trial_reports={sha: {"trials": [trial],
                                 "report_sha256": report_sha}},
            now=NOW,
        )
        assert out["to"] == "screened", out
        # The screen holds: a new family name is a human decision, so
        # the variant never enters shadow.
        out = advance_one(store, candidates={sha: (payload, True)}, now=NOW)
        assert out["status"] == "nothing_to_advance", out
        current = get_experiment(store, exp["experiment_id"])
        assert current["status"] == "screened"
        assert not [e for e in current["outcome_json"]
                    if e.get("stage") == "shadow-family"]
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
        assert "human" in findings[0]["summary"]
        # A second tick records no duplicate finding.
        out = advance_one(store, candidates={sha: (payload, True)}, now=NOW)
        assert out["status"] == "nothing_to_advance", out
        assert len(list_findings(store, exp["experiment_id"])) == 1
    finally:
        store.close()


def test_new_family_variant_with_failing_artifact_is_discarded(tmp_path):
    store = _store(tmp_path)
    try:
        design = SCHEDULED_VARIANTS[1]  # gbm-trees-150
        sha = design_sha256(design)
        exp = create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=copy.deepcopy(design), proposal_sha256=sha, now=NOW,
        )
        doc = _gbm_doc(sha, asset="ETH")
        doc["gates_passed"] = False
        payload = json.dumps(doc).encode()
        trial = _passing_trial(sha, design=design, asset="ETH")
        out = advance_one(store, now=NOW)
        assert out["to"] == "ablating", out
        report_sha = hashlib.sha256(b"gbm-report-fail").hexdigest()
        out = advance_one(
            store,
            trial_reports={sha: {"trials": [trial],
                                 "report_sha256": report_sha}},
            now=NOW,
        )
        assert out["to"] == "screened", out
        # A failing artifact is discarded, not held: the hold is only
        # for artifacts that pass the screen.
        out = advance_one(store, candidates={sha: (payload, True)}, now=NOW)
        assert out["status"] == "advanced" and out["to"] == "discarded", out
        findings = list_findings(store, exp["experiment_id"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "negative_result"
        assert "human" not in findings[0]["summary"]
    finally:
        store.close()


def test_llm_cannot_propose_scheduled_variant():
    proposal = {"model": MODEL, "hypothesis": SCHEDULED_HYPOTHESIS,
                "research_only": True, "live_model_promoted": False,
                "retrospective_screen": "not_tested", "prediction": None,
                "feature_indices": None,
                "rationale": "a scheduled design, not an LLM mask",
                "falsification": "the grid does not contain this design"}
    assert validate_proposal(proposal) is False
    # Positive control: the same shape with an LLM hypothesis validates.
    assert validate_proposal({**proposal, "hypothesis": "volume_only"}) is True


def test_screen_scheduled_artifact():
    sha = design_sha256(SCHEDULED_VARIANTS[0])
    assert screen_scheduled_artifact(
        json.dumps(_archive_doc(sha)).encode()) == {
            "retrospective_screen": "passed"}
    gbm_sha = design_sha256(SCHEDULED_VARIANTS[1])
    assert screen_scheduled_artifact(
        json.dumps(_gbm_doc(gbm_sha)).encode()) == {
            "retrospective_screen": "passed"}
    volcal_sha = design_sha256(SCHEDULED_VARIANTS[4])
    assert screen_scheduled_artifact(
        json.dumps(_volcal_doc(volcal_sha)).encode()) == {
            "retrospective_screen": "passed"}
    assert screen_scheduled_artifact(b"not json") == {
        "retrospective_screen": "insufficient",
        "reason": "artifact payload is not JSON"}
    assert screen_scheduled_artifact(
        json.dumps({"schema": "unknown"}).encode())[
            "retrospective_screen"] == "insufficient"
    gated = _archive_doc(sha)
    gated["gates_passed"] = False
    assert screen_scheduled_artifact(
        json.dumps(gated).encode())["retrospective_screen"] == "insufficient"
    stale = _archive_doc(sha)
    stale["trained_through"] = "2999-01-01T00:00:00+00:00"
    assert screen_scheduled_artifact(
        json.dumps(stale).encode())["retrospective_screen"] == "insufficient"


def test_volcal_grid_entries_resolve_to_volcal_training():
    """Every volcal grid entry loads, parses, and resolves to the real
    volcal feature/training code — no placeholders, no new family."""
    from parlay_bot.research.volcal_features import GROUPS as VGROUPS

    volcal_entries = [d for d in SCHEDULED_VARIANTS
                      if d["model_shape"] == "volcal-offline-v1"]
    assert [d["variant_id"] for d in volcal_entries] == [
        "volcal-full", "volcal-no-vol", "volcal-no-compression",
        "volcal-no-volume", "volcal-no-dc",
    ]
    for design in volcal_entries:
        sha = design_sha256(design)
        assert lookup_variant(sha) is design
        assert design["variant_kind"] == "feature_group_off"
        assert design["train_window"] == "common-2021"
        assert design["gbm_params"] is None
        assert design["shadow_mode"] == "artifact"
        for group in design["feature_groups_off"]:
            assert group in VGROUPS and group != "candle"
        # The training module accepts exactly these ablations.
        volcal_training._check_groups_off(design["feature_groups_off"])
        doc = _volcal_doc(sha, asset=design["asset"],
                          groups_off=design["feature_groups_off"])
        assert screen_scheduled_artifact(
            json.dumps(doc).encode()) == {"retrospective_screen": "passed"}
        identity = scheduled_shadow_identity(design, json.dumps(doc).encode())
        assert identity["family"] == "volcal_v1"
        assert identity["asset"] == design["asset"]
        assert identity["weights_sha256"] == doc["weights_sha256"]


def test_volcal_scheduled_variant_closes_discarded_end_to_end(tmp_path):
    store = _store(tmp_path)
    try:
        design = SCHEDULED_VARIANTS[4]  # volcal-full
        sha = design_sha256(design)
        exp = create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=copy.deepcopy(design), proposal_sha256=sha, now=NOW,
        )
        doc = _volcal_doc(sha)
        _drive_scheduled_to_shadow(
            store, design_sha=sha,
            trial=_passing_trial(sha, design=design),
            artifact_payload=json.dumps(doc).encode())
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


async def test_weekly_archive_trains_volcal_scheduled_variant(tmp_path, monkeypatch):
    """The weekly job routes a volcal grid design to volcal_training
    (not archive): the result doc is a volcal_v1 artifact/report
    carrying the design SHA and a scheduled trial status."""
    from parlay_bot.research import weekly_archive
    from tests.test_archive_model import _frames, _splits

    drive = _FakeDrive()
    monkeypatch.setattr(weekly_archive, "_drive", lambda _: drive)
    store = _store(tmp_path)
    try:
        design = SCHEDULED_VARIANTS[4]  # volcal-full
        sha = design_sha256(design)
        exp = create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=copy.deepcopy(design), proposal_sha256=sha, now=NOW,
        )
        request = scheduled_request(exp)
        train_end, test_start, trained_through = _splits()
        frames = _frames()
        training = {
            "asset": "BTC", "horizon": 5, "train_end": train_end,
            "test_start": test_start, "trained_through": trained_through,
            "provenance": PROV, "code_version": "test",
        }
        common = weekly_archive._window(
            frames, through=trained_through, first="2021-01-01")
        full = weekly_archive._window(
            frames, through=trained_through, first=None)
        await weekly_archive._train_variant(
            drive, request, common=common, full=full, training=training,
        )
        name = f"scheduled-result-{exp['experiment_id']}.json"
        kind = next(
            (k for k in ("model", "report") if name in drive.files[k]),
            None,
        )
        assert kind is not None
        doc = json.loads(drive.files[kind][name])
        assert doc["family"] == "volcal_v1"
        assert doc["scheduled_design_sha256"] == sha
        assert doc["scheduled_trial"]["status"] in (
            "passed", "rejected", "blocked")
        if doc["scheduled_trial"]["status"] == "passed":
            assert kind == "model"
            volcal_training.validate(doc)
    finally:
        store.close()


def test_candidate_handoff_accepts_scheduled_design_sha():
    sha = "d" * 64
    payload = json.dumps({"scheduled_design_sha256": sha,
                          "schema": "rival-archive-model-v1"}).encode()
    assert _candidate_from_payload(payload) == sha
    payload = json.dumps(
        {"model_spec": {"scheduled_design_sha256": sha}}).encode()
    assert _candidate_from_payload(payload) == sha
    # LLM path unchanged.
    payload = json.dumps(
        {"model_spec": {"lfm3_proposal_sha256": "e" * 64}}).encode()
    assert _candidate_from_payload(payload) == "e" * 64
    assert _candidate_from_payload(b"not json") is None


def test_scheduled_path_spends_no_llm_calls_by_construction():
    for fn in (open_scheduled_variant, advance_one,
               drive_research_loop_tick, fetch_loop_inputs):
        params = inspect.signature(fn).parameters
        assert "ai" not in params
        assert "llm" not in params


async def test_async_tick_opens_scheduled_variant(tmp_path):
    store = _store(tmp_path)
    try:
        drive = _FakeDrive()
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "opened", out
        assert out["variant_id"] == "archive-premium-off"
        # One action per tick: the next tick advances the open variant
        # instead of opening another.
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "advanced", out
        assert out["to"] == "ablating"
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "submitted", out
        # Nothing to advance (no trial report yet): the tick opens the
        # next grid entry.
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "opened", out
        assert out["variant_id"] == "gbm-trees-150"
        # Weekly cap spent: later ticks open nothing.
        await drive_research_loop_tick(store, drive=drive, now=NOW)
        await drive_research_loop_tick(store, drive=drive, now=NOW)
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "nothing_to_advance", out
    finally:
        store.close()


@pytest.mark.parametrize("has_history", [True, False])
async def test_weekly_archive_job_resolves_the_requested_attempt(tmp_path, monkeypatch, has_history):
    from parlay_bot.perpetuals.archive_model import ArchiveModelSet, archive_calls, model_sha256
    from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.research import weekly_archive
    from tests.test_archive_model import _frames, _splits

    drive = _FakeDrive()
    monkeypatch.setattr(weekly_archive, "_drive", lambda _: drive)
    store = _store(tmp_path)
    try:
        first = await drive_research_loop_tick(store, drive=drive, now=NOW)
        exp_id = first["opened"]
        assert (await drive_research_loop_tick(store, drive=drive, now=NOW))["to"] == "ablating"
        assert (await drive_research_loop_tick(store, drive=drive, now=NOW))["status"] == "submitted"

        train_end, test_start, trained_through = _splits()
        frames = _frames() if has_history else {}
        training = {
            "asset": "BTC", "horizon": 5, "train_end": train_end, "test_start": test_start,
            "trained_through": trained_through, "provenance": PROV, "code_version": "test",
        }
        await weekly_archive.train_weekly_archive(
            frames, training=training, census={}, out=tmp_path, publish=True,
        )
        assert (tmp_path / "archive-v1-BTC-5m-report.json").exists()
        assert (tmp_path / "gbm-v1-BTC-5m-report.json").exists()
        assert await weekly_archive.next_scheduled_request(drive, "BTC") is None

        store.close()
        store = _store(tmp_path)
        outcome = await drive_research_loop_tick(store, drive=drive, now=NOW)
        if not has_history:
            assert outcome["to"] == "discarded"
            await drive_research_loop_tick(store, drive=drive, now=NOW)
            findings = list_findings(store, exp_id)
            assert len(findings) == 1 and "blocked" in findings[0]["summary"]
            return

        assert outcome["to"] == "screened"
        assert (await drive_research_loop_tick(store, drive=drive, now=NOW))["to"] == "shadow"
        assert "gbm-v1-BTC-5m-model.json" in drive.files["model"]
        file_name = f"scheduled-result-{exp_id}.json"
        artifact = drive.files["model"][file_name]
        doc = json.loads(artifact)
        assert doc["feature_groups_off"] == ["premium"]
        models = ArchiveModelSet(store)
        assert models.import_document(file_name, artifact) == 1
        assert models.import_document(file_name, artifact) == 0

        opened = NOW - timedelta(minutes=35)
        issued = NOW - timedelta(minutes=30)
        target = NOW - timedelta(minutes=25)
        settled = NOW - timedelta(minutes=19)
        values = [0.1] * len(FEATURES)
        call, = archive_calls(models, asset="BTC", horizon=5,
                              vector=(values, (1.0,) * len(GROUPS)), now=issued)
        assert call["model_sha256"] == model_sha256(doc)
        for i in GROUPS["premium"]:
            values[i] = 1000.0
        assert archive_training.probability(doc, values) == call["probability"]

        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        competition = ShadowModelCompetition(store)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,target_opened_at,
                     probability_up,baseline_up,training_evaluated,settled_at,outcome_up)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL, opened.isoformat(), 100.0,
                 issued.isoformat(), target.isoformat(), 0.6, 0.45, 100, settled.isoformat(), 1),
            )
            assert competition.issue(
                conn, samples=[], features=(0.25,) * 10, now=issued,
                venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
                horizon=5, reference_opened_at=opened, target_opened_at=target, issued_at=issued,
                incumbent_up=0.6, baseline_up=0.45, challengers=[call],
            ) == 1
        assert competition.settle(now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute("SELECT training_sha256, settled_at FROM rival_model_shadow_forecasts"
                               " WHERE family='archive_v1'").fetchone()
        assert row["training_sha256"] == call["model_sha256"]
        assert row["settled_at"] == settled.isoformat()
        assert get_experiment(store, exp_id)["status"] == "shadow"
    finally:
        store.close()


def test_weekly_cli_reports_empty_restore_for_both_families(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    census = tmp_path / "census.json"
    census.write_text("{}", encoding="utf-8")
    out = tmp_path / "out"
    status = archive_training.main([
        "--asset", "BTC", "--horizon", "5", "--manifests-dir", str(manifests),
        "--census", str(census), "--binance-cache", str(tmp_path / "binance"),
        "--binance-last-month", "2026-10", "--train-end", "2026-09-11T00:00:00+00:00",
        "--test-start", "2026-10-09T00:00:00+00:00",
        "--trained-through", "2026-11-06T23:55:00+00:00", "--out", str(out),
    ], weekly=True)
    assert status == 0
    for name in ("archive-v1-BTC-5m-report.json", "gbm-v1-BTC-5m-report.json"):
        report = json.loads((out / name).read_text(encoding="utf-8"))
        assert report["training_status"] == "blocked"
    assert not list(out.glob("*-model.json"))
