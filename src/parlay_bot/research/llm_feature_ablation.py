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
from parlay_bot.perpetuals.research_hypotheses import ASSETS, HYPOTHESES, MODEL
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


def evaluate_hypothesis_ablations(root, *, qualified_streams, proposals):
    """Score separate pretrained masks against the SAME existing baseline.

    Only the training partition updates model coefficients. The purged
    validation period measures the LLM-suggested fixed mask. The sealed
    holdout is never examined, and no artifact is placed in the import feed.
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
                active=MASKS[suggestion["hypothesis"]],
            )
            alternate = _score(validation, weights, rate)
            trials.append({
                "asset": asset, "venue": identity["venue"],
                "contract": identity["contract"], "quote": identity["quote"],
                "horizon_minutes": horizon,
                "hypothesis": suggestion["hypothesis"],
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
            })
    return trials
