"""Pre-registered, validation-only experiments from LFM3 feature hypotheses.

All outcomes come from verified original source candles. LLM output selects
only a fixed feature mask, never a probability, label, model weight or trade.
The already-inspected historical window is exploratory, not independent proof.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.markets import HORIZONS
from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT, TARGET_CONTRACT, canonical
from parlay_bot.perpetuals.research_hypotheses import (
    ASSETS,
    HYPOTHESES,
    MODEL,
    valid_feature_indices,
)
from parlay_bot.research.candidate_registry import verified_candidate
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import assemble
from parlay_bot.research.live_training import (
    _fit,
    _score,
    compatible_partitions,
    historical_features,
    qualified_manifest_groups,
)

MASKS = {
    "trend_only": (0, 1, 5, 6),
    "momentum_only": (0, 1, 5, 6, 7),
    "volatility_only": (2, 3, 8),
    "volume_only": (4, 9),
}


def verify_proposals(receipt: object, *, venue: str, now=None) -> list[dict]:
    """Reject forged, stale, nonlocal or unfenced model hypotheses."""
    if not isinstance(receipt, dict) or receipt.get("status") != "verified":
        return []
    if receipt.get("source") != "live_postgresql_llm_hypotheses":
        return []
    rows = receipt.get("proposals")
    if not isinstance(rows, list) or len(rows) > 4:
        raise ValueError("unbounded AI research proposals")
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("UTC timestamp required")
    accepted, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("invalid AI hypothesis record")
        asset, hypothesis = row.get("asset"), row.get("hypothesis")
        venues = row.get("venues")
        if (asset not in ASSETS or hypothesis not in HYPOTHESES
            or asset in seen or row.get("model") != MODEL
            or (not valid_feature_indices(row.get("feature_indices"))
                if hypothesis == "feature_subset_v2"
                else row.get("feature_indices") is not None)
            or not isinstance(venues, list) or venue not in venues
            or not all(isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]{2,24}", v)
                       for v in venues)
            or not isinstance(row.get("evidence_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["evidence_sha256"])):
            raise ValueError("unverified AI feature identity")
        try:
            stamp = datetime.fromisoformat(row["slot_utc"]).astimezone(UTC)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid AI proposal timestamp") from exc
        if not timedelta(0) <= clock.astimezone(UTC) - stamp <= timedelta(days=7):
            raise ValueError("stale or future AI proposal")
        identity = {
            "asset": asset, "slot_utc": row["slot_utc"],
            "model": MODEL, "evidence_sha256": row["evidence_sha256"],
            "hypothesis": hypothesis, "venues": venues,
        }
        if hypothesis == "feature_subset_v2":
            identity["feature_indices"] = row["feature_indices"]
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest != row.get("record_sha256"):
            raise ValueError("AI proposal digest mismatch")
        accepted.append({**identity, "record_sha256": digest})
        seen.add(asset)
    return accepted


def _asset_for_contract(contract: str) -> str | None:
    value = contract.upper()
    for asset in ASSETS:
        if value.startswith(asset) or (asset == "BTC" and value.startswith("XBT")):
            return asset
    return None


def evaluate_hypothesis_ablations(root, *, qualified_streams, proposals, candidate_sink=None):
    """Score separate pretrained masks against the SAME existing baseline.

    Only the training partition updates model coefficients. The purged
    validation period measures the LLM-suggested fixed mask. The sealed
    holdout is never examined in validation-only mode. When a separate
    candidate sink is supplied, only masks that first beat the ordinary
    logistic model on purged validation may open the sealed test. Passing
    both still means *research only*, never promoted weights or a trade.
    """
    selected = {item["asset"]: item for item in proposals}
    if not selected:
        return []
    trials = []
    for paths in qualified_manifest_groups(root, qualified_streams=qualified_streams):
        identity, rows, provenance = assemble(paths)
        asset = _asset_for_contract(identity["contract"])
        suggestion = selected.get(asset)
        if suggestion is None or identity["venue"] not in suggestion["venues"]:
            continue
        vectors = historical_features(rows, identity)
        active_mask = (tuple(suggestion["feature_indices"])
                       if suggestion["hypothesis"] == "feature_subset_v2"
                       else MASKS[suggestion["hypothesis"]])
        for horizon in HORIZONS:
            try:
                train, validation, _sealed_test = compatible_partitions(
                    rows, vectors, horizon=horizon, identity=identity,
                )
            except HistoryImportError:
                continue
            base_weights, base_rate = _fit(train, l2=.001)
            baseline = _score(validation, base_weights, base_rate)
            weights, rate = _fit(
                train, l2=.005, rate=.01, epochs=4,
                active=active_mask,
            )
            alternate = _score(validation, weights, rate)
            trial = {
                "asset": asset, "venue": identity["venue"],
                "contract": identity["contract"], "quote": identity["quote"],
                "horizon_minutes": horizon,
                "hypothesis": suggestion["hypothesis"],
                "feature_indices": list(active_mask),
                "proposal_sha256": suggestion["record_sha256"],
                "original_source_months": len(provenance["months"]),
                "training_count": len(train),
                "validation_count": len(validation),
                "baseline_validation_brier": baseline["brier"],
                "llm_mask_validation_brier": alternate["brier"],
                "validation_brier_delta": (
                    baseline["brier"] - alternate["brier"]
                ),
                "llm_suggested_research_only": True,
                "holdout_examined": False,
                "candidate_imported": False,
                "forward_edge_verified": False,
                "after_cost_edge_verified": False,
            }
            trials.append(trial)
            if candidate_sink is None:
                continue
            # Open the sealed holdout only AFTER a single, preselected
            # feature mask passes validation vs BOTH the smoothed historical
            # class prior and ordinary full-feature logistic. Never search
            # the holdout for a more favorable LLM hypothesis.
            valid = (
                len(provenance["months"]) >= 6
                and alternate["count"] >= 100
                and alternate["days"] >= 7
                and alternate["brier_gain"] > .005
                and alternate["ece_10"] <= .10
                and baseline["brier"] - alternate["brier"] > .005
            )
            trial["passed_purged_validation"] = bool(valid)
            if not valid:
                trial["retrospective_candidate_status"] = "validation_rejected"
                continue
            holdout_scores = _score(_sealed_test, weights, rate)
            ordinary_holdout = _score(_sealed_test, base_weights, base_rate)
            trial["holdout_examined"] = True
            trial["holdout_brier_delta"] = (
                ordinary_holdout["brier"] - holdout_scores["brier"]
            )
            if trial["holdout_brier_delta"] <= .005:
                trial["retrospective_candidate_status"] = "ordinary_holdout_not_beaten"
                continue
            spec = {
                "model": "offline-live-contract-logistic-v3",
                "method": "lfm3-hypothesis-" + suggestion["hypothesis"],
                "identity": identity,
                "horizon_minutes": horizon,
                "feature_contract": FEATURE_CONTRACT,
                "target_contract": TARGET_CONTRACT,
                "weights": [round(w, 12) for w in weights],
                "train_base_rate": rate,
                "training_count": len(train),
                "training_cutoff": max(s[1] for s in train).isoformat(),
                "validation_cutoff": _sealed_test[0][0].isoformat(),
                "evaluation_completed_through": max(
                    s[1] for s in _sealed_test
                ).isoformat(),
                "source_sha256_by_month": provenance["source_sha256_by_month"],
                "provider_sha256_by_month": provenance.get(
                    "provider_sha256_by_month", {}),
                "months_without_provider_hash": provenance.get(
                    "months_without_provider_hash", []),
                "lfm3_proposal_sha256": suggestion["record_sha256"],
                "lfm3_model": MODEL,
            }
            if suggestion["hypothesis"] == "feature_subset_v2":
                spec["lfm3_feature_indices"] = list(active_mask)
            artifact = {
                "research_only": True, "live_model_promoted": False,
                "model_sha256": hashlib.sha256(
                    canonical(spec).encode()
                ).hexdigest(),
                "model_spec": spec,
                "validation": alternate,
                "out_of_sample_test": holdout_scores,
            }
            admission = verified_candidate(canonical(artifact).encode())
            trial["retrospective_candidate_status"] = (
                "screened_for_future_shadow"
                if admission["retrospective_screen"] == "passed"
                else admission["admission_reason"]
            )
            if admission["retrospective_screen"] != "passed":
                continue
            # Only screened research artifacts can reach the existing
            # import folder and prospectively compete. No auto-promotion.
            candidate_sink.append({
                **artifact, "schema_version": "rival-live-compatible-research-v3",
                "research_candidate_passed": True,
                "compared_on_validation": [{
                    "method": spec["method"],
                    "scores": alternate,
                    "training_count": len(train),
                }],
                "prospective_accuracy": None,
            })
    return trials
