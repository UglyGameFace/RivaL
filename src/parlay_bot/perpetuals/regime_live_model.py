"""Runtime serving set for the regime live-learning challengers.

Spec rival-regime-live-learning-spec-20261108, D1-D12, plus the Q1
amendment (Felix, 2026-11-08): two live families that learn as outcomes
settle, instead of an offline weekly fit:

  regime_live_v1       base family, learning rate 0.025 (the incumbent's);
  regime_live_fast_v1  fast family, learning rate 0.05 (2x the base).

Each family is a per-stream online logistic over the incumbent's 10 live
features, trained ONLY on settled labels whose issuance bar was trending
(Kaufman efficiency ratio over the last 20 closed 5-minute bars > 0.30,
frozen at issuance -- D3), with no Platt scaling (D2). The weights start
as a one-time copy of the incumbent's per-stream weights (D4, warm start
recorded in the checkpoint state) and then move by SGD on trending
labels only; the incumbent keeps learning on every label and is never
touched by either family (D2/D3).

Both families reuse ``rival_live_model_checkpoints`` under their own
feature-contract string (D5): no new table, no pin table, and the
weights SHA in the shadow ledger is the live continuation the
``selected()`` continuity branch checks -- NOT a D14 pin (D6). Each
family has its own kill switch (``forward_audit.
ISSUE_REGIME_LIVE_V1_CHALLENGERS`` / ``ISSUE_REGIME_LIVE_FAST_V1_
CHALLENGERS``, both default True, D8) and its own Route B promotion
clock (150 targets + 21 days from first issuance, D9). Neither family
ever appears in the OTG display (D11).

Offline regime_v2 is retired by the same change (D10): its kill switch
is off and it no longer trains in the weekly archive loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.markets import asset_for_contract
from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT as LIVE_CONTRACT
from parlay_bot.perpetuals.regime_gate import (
    ER_THRESHOLD,
    ER_WINDOW,
    efficiency_ratio,
)

_LOG = logging.getLogger(__name__)

BASE_FAMILY = "regime_live_v1"
FAST_FAMILY = "regime_live_fast_v1"
FAMILIES = (BASE_FAMILY, FAST_FAMILY)

BASE_LEARNING_RATE = 0.025
FAST_LEARNING_RATE = 0.05

# The feature contract pins the training identity (D5): the incumbent's
# 10 live features + the trending-label rule + the learning rate. The
# rate is part of the contract so the two families keep separate
# checkpoint rows even though they read the same features.
BASE_FEATURE_CONTRACT = f"{LIVE_CONTRACT}+trending-er20-gt0.30+lr0.025"
FAST_FEATURE_CONTRACT = f"{LIVE_CONTRACT}+trending-er20-gt0.30+lr0.05"

FAMILY_CONFIG = {
    BASE_FAMILY: {
        "feature_contract": BASE_FEATURE_CONTRACT,
        "learning_rate": BASE_LEARNING_RATE,
        "metric_prefix": "regime_live",
    },
    FAST_FAMILY: {
        "feature_contract": FAST_FEATURE_CONTRACT,
        "learning_rate": FAST_LEARNING_RATE,
        "metric_prefix": "regime_live_fast",
    },
}

TRIAL_DAYS = 30


def feature_contract(family: str) -> str:
    """The checkpoint feature-contract string for a live family."""
    return FAMILY_CONFIG[family]["feature_contract"]


def learning_rate(family: str) -> float:
    """The SGD learning rate for a live family."""
    return FAMILY_CONFIG[family]["learning_rate"]


def metric_prefix(family: str) -> str:
    """The cycle-metrics prefix for a live family (regime_live / regime_live_fast)."""
    return FAMILY_CONFIG[family]["metric_prefix"]


def feature_digest(family: str) -> str:
    """Continuity digest for ``selected()`` (D5/D6).

    Pins the family, the gate, the feature contract (which itself pins
    the trending rule and the learning rate) and the 10 live features.
    A MODEL bump in forward_audit.py changes the live model version and
    therefore the warm-start source, so it deliberately breaks
    continuity instead of silently serving a different decider under
    the same family name -- the same rule regime_v1's digest follows.
    """
    return hashlib.sha256("|".join((
        family,
        "efficiency-ratio-20",
        "er>0.30",
        FAMILY_CONFIG[family]["feature_contract"],
        "10-features",
    )).encode()).hexdigest()


def trending_samples(candles, samples):
    """The settled samples whose issuance bar was trending (D3).

    The efficiency ratio is recomputed deterministically from the
    persisted candles: the 21 closes ending at the issuance bar
    (``issued_at`` is that bar's ``observed_at``), so a restart or a
    replay over the same candles admits exactly the same samples.
    A sample with no issuance bar in the persisted window, or fewer
    than 21 closes ending at it, is not trained on. Runs in
    O(bars + 21 * samples).
    """
    if not candles or not samples:
        return ()
    ordered = sorted(candles, key=lambda c: utc(c.opened_at))
    closes = [c.close_price for c in ordered]
    observed = [utc(c.observed_at) for c in ordered]
    by_observed = {}
    for idx, stamp in enumerate(observed):
        by_observed[stamp] = idx  # last wins, as live_examples dedupes
    out = []
    for sample in samples:
        idx = by_observed.get(utc(sample.issued_at))
        if idx is None:
            # A batch-revealed bar can share an observed_at with the
            # window's latest candle; fall back to the last candle
            # observed at or before issuance.
            idx = max(
                (i for i, stamp in enumerate(observed) if stamp <= utc(sample.issued_at)),
                default=None,
            )
            if idx is None:
                continue
        if idx < ER_WINDOW:
            continue
        er = efficiency_ratio(closes[idx - ER_WINDOW:idx + 1])
        if er is not None and er > ER_THRESHOLD:
            out.append(sample)
    return tuple(out)


def regime_live_calls(*, family, candles, probability, training_rows,
                      model_sha256):
    """One shadow call for a live family, or [] when it must not issue.

    Same validation shape as regime_calls(): the gate must pass on the
    live candle list, the probability must be a strict interior value
    (a 0/1 forecast is an abstention, never a call), and the weights
    SHA must be a 64-hex digest. The probability is the family's own
    live-learner probability, not the incumbent's (unlike v1, which
    reuses the incumbent's verbatim).
    """
    from parlay_bot.perpetuals.regime_gate import trending_gate

    if family not in FAMILY_CONFIG:
        return []
    if (not trending_gate(candles)
            or probability is None
            or not 0 < float(probability) < 1
            or not isinstance(model_sha256, str)
            or len(model_sha256) != 64):
        return []
    return [{"family": family,
             "probability": float(probability),
             "training_rows": int(training_rows),
             "model_sha256": model_sha256}]


def artifact_identity(store, asset: str, horizon: int, *, family: str) -> dict | None:
    """Live identity for (asset, horizon, family), or None.

    The continuity identity for the ``selected()`` branch (D5/D6):
    feature digest, weights SHA recomputed from the stored checkpoint
    weights (never trusted from a stored column), trained-through, and
    the graded count read live from the shadow ledger -- the number of
    settled rows for this family under its current weights SHA. A
    weight change is accepted only with more graded learning behind
    it (the live learner moved), which is exactly the regime_v1
    continuity rule.
    """
    if family not in FAMILY_CONFIG:
        return None
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT contract, weights_sha256, trained_at, state_json
                   FROM rival_live_model_checkpoints
                   WHERE feature_contract=? AND horizon_minutes=?""",
                (FAMILY_CONFIG[family]["feature_contract"], int(horizon)),
            ).fetchall()
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    best = None
    for row in rows:
        if asset_for_contract(row["contract"]) != asset:
            continue
        if best is None or row["trained_at"] > best["trained_at"]:
            best = row
    if best is None:
        return None
    try:
        state = json.loads(best["state_json"])
        weights = state["weights"]
        weights_sha = hashlib.sha256(
            json.dumps(weights, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    except (ValueError, KeyError, TypeError):
        return None
    try:
        with store.connect() as conn:
            graded = conn.execute(
                """SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                   AND training_sha256=?
                   AND settled_at IS NOT NULL AND outcome_up IN (0,1)""",
                (family, asset, int(horizon), weights_sha),
            ).fetchone()
        graded_count = int(graded["n"]) if graded is not None else 0
    except Exception:  # noqa: BLE001 - fail closed to the incumbent
        return None
    return {"feature_digest": feature_digest(family),
            "weights_sha256": weights_sha,
            "trained_through": best["trained_at"],
            "graded_count": graded_count}


def calibration_status(store, asset: str, horizon: int, *, family: str) -> dict:
    """Calibration record for receipts; the live learner has no Platt fit.

    The live families train without Platt scaling (D2), so there is no
    calibration artifact to report. Route B's calibration screen is
    the evidence for these families, not a stored calibration.
    """
    return {"method": "none", "status": "unevaluated"}


def _weight_drift(store, asset: str, horizon: int, *, family: str) -> dict:
    """Weight drift vs the warm-start copy, per stream (D7).

    ``weight_drift`` is the L2 distance ||w - w0||_2 between the live
    weights and the warm-start weights recorded at the first fit; the
    mean and max across the family's streams for (asset, horizon), and
    ``updates`` the total trending labels applied. Returns zeros when
    the family has no checkpoint yet.
    """
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT contract, state_json
                   FROM rival_live_model_checkpoints
                   WHERE feature_contract=? AND horizon_minutes=?""",
                (FAMILY_CONFIG[family]["feature_contract"], int(horizon)),
            ).fetchall()
    except Exception:  # noqa: BLE001 - display only, never raise
        return {"updates": 0, "weight_drift_mean": 0.0, "weight_drift_max": 0.0}
    drifts = []
    updates = 0
    for row in rows:
        if asset_for_contract(row["contract"]) != asset:
            continue
        try:
            state = json.loads(row["state_json"])
            weights = state["weights"]
            warm = state["warm_start_weights"]
            updates += int(state["updates"])
        except (ValueError, KeyError, TypeError):
            continue
        if len(weights) != len(warm):
            continue
        drifts.append(math.sqrt(sum((w - w0) ** 2 for w, w0 in zip(weights, warm))))
    if not drifts:
        return {"updates": updates, "weight_drift_mean": 0.0, "weight_drift_max": 0.0}
    return {"updates": updates,
            "weight_drift_mean": sum(drifts) / len(drifts),
            "weight_drift_max": max(drifts)}


def trial_report(store, *, asset, horizon, family, days=TRIAL_DAYS, now=None) -> dict:
    """D7 trial report for a live family over the last `days` days.

    issued / graded / decided / hits / windows / model_brier /
    incumbent_brier / baseline_brier are the same columns as the pooled
    trial row, measured on the family's issued subset only. The
    decomposition is the extra part, and for the live families the
    paired gain vs the incumbent is the headline (unlike v1, where it
    is 0.000 by construction -- v1 borrows the incumbent's weights, so
    it cannot beat them; the live families train their own):
      paired_gain            = incumbent Brier - family Brier on the subset
                               (positive: the family beat the incumbent on
                               the targets it actually issued on);
      paired_gain_vs_regime_v1 = regime_v1 Brier - family Brier on the
                               targets both families issued on (the
                               head-to-head: did live learning add
                               anything over gating alone?);
      selection_effect       = incumbent Brier on the subset - incumbent
                               Brier on ALL targets in the same window
                               (positive: the subset was harder than
                               average for the incumbent; negative:
                               easier);
      coverage               = issued family rows / issued incumbent rows
                               in the window;
      availability_ratio     = issued family rows / issued regime_v1 rows
                               in the window (share of trending candles
                               the family issued on -- the second
                               coverage ratio);
      updates                = trending labels applied to the family's
                               weights (all time, per stream summed);
      weight_drift           = L2 distance from the warm-start copy
                               (mean / max across streams);
      paired_gain_last_7d / paired_gain_days_8_30 = the paired gain
                               split into the last 7 days vs days 8-30
                               (is the edge recent, or did it fade?).
    Never raises: returns {"issued": 0} on any read failure.
    """
    stamp = utc(now or datetime.now(UTC))
    since = (stamp - timedelta(days=days)).isoformat()
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS issued,
                          SUM(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL THEN 1 ELSE 0 END) AS graded,
                          SUM(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1) THEN 1 ELSE 0 END) AS decided,
                          SUM(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   AND ((probability_up >= 0.5) = (outcome_up = 1))
                                   THEN 1 ELSE 0 END) AS hits,
                          COUNT(DISTINCT CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   THEN target_opened_at END) AS windows,
                          AVG(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   THEN model_brier END) AS model_brier,
                          AVG(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   THEN incumbent_brier END) AS incumbent_brier,
                          AVG(CASE WHEN settled_at IS NOT NULL
                                   AND outcome_up IS NOT NULL
                                   AND probability_up NOT IN (0,1)
                                   AND baseline_brier IS NOT NULL
                                   THEN baseline_brier END) AS baseline_brier,
                          MIN(issued_at) AS first_issued
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family=?
                     AND issued_at>=?""",
                (asset, int(horizon), family, since),
            ).fetchone()
            if row is None or not row["issued"]:
                return {"issued": 0}
            issued = int(row["issued"])
            model_brier = row["model_brier"]
            incumbent_brier = row["incumbent_brier"]
            paired_gain = (
                None if model_brier is None or incumbent_brier is None
                else float(incumbent_brier) - float(model_brier)
            )
            v1 = conn.execute(
                """SELECT AVG(r1.model_brier) AS v1_brier,
                          AVG(r1.incumbent_brier) AS v1_incumbent_brier,
                          COUNT(*) AS n
                   FROM rival_model_shadow_forecasts r1
                   JOIN rival_model_shadow_forecasts r2
                     ON r1.asset = r2.asset
                    AND r1.horizon_minutes = r2.horizon_minutes
                    AND r1.target_opened_at = r2.target_opened_at
                    AND r1.venue = r2.venue
                    AND r1.contract = r2.contract
                   WHERE r1.family='regime_v1' AND r2.family=?
                     AND r1.asset=? AND r1.horizon_minutes=?
                     AND r1.issued_at>=? AND r2.issued_at>=?
                     AND r1.settled_at IS NOT NULL
                     AND r1.outcome_up IS NOT NULL
                     AND r1.probability_up NOT IN (0,1)
                     AND r2.settled_at IS NOT NULL
                     AND r2.outcome_up IS NOT NULL
                     AND r2.probability_up NOT IN (0,1)""",
                (family, asset, int(horizon), since, since),
            ).fetchone()
            paired_gain_vs_regime_v1 = None
            if v1 is not None and v1["n"]:
                head_to_head = conn.execute(
                    """SELECT AVG(r2.model_brier) AS live_brier
                       FROM rival_model_shadow_forecasts r1
                       JOIN rival_model_shadow_forecasts r2
                         ON r1.asset = r2.asset
                        AND r1.horizon_minutes = r2.horizon_minutes
                        AND r1.target_opened_at = r2.target_opened_at
                        AND r1.venue = r2.venue
                        AND r1.contract = r2.contract
                       WHERE r1.family='regime_v1' AND r2.family=?
                         AND r1.asset=? AND r1.horizon_minutes=?
                         AND r1.issued_at>=? AND r2.issued_at>=?
                         AND r1.settled_at IS NOT NULL
                         AND r1.outcome_up IS NOT NULL
                         AND r1.probability_up NOT IN (0,1)
                         AND r2.settled_at IS NOT NULL
                         AND r2.outcome_up IS NOT NULL
                         AND r2.probability_up NOT IN (0,1)""",
                    (family, asset, int(horizon), since, since),
                ).fetchone()
                if (v1["v1_brier"] is not None and head_to_head is not None
                        and head_to_head["live_brier"] is not None):
                    paired_gain_vs_regime_v1 = (
                        float(v1["v1_brier"]) - float(head_to_head["live_brier"])
                    )
            incumbent_all = conn.execute(
                """SELECT AVG(model_brier) AS brier
                   FROM perp_forward_audit
                   WHERE asset=? AND horizon_minutes=? AND issued_at>=?
                     AND settled_at IS NOT NULL AND outcome_up IN (0,1)""",
                (asset, int(horizon), since),
            ).fetchone()
            selection_effect = None
            if (incumbent_brier is not None and incumbent_all is not None
                    and incumbent_all["brier"] is not None):
                selection_effect = (
                    float(incumbent_brier) - float(incumbent_all["brier"])
                )
            counts = conn.execute(
                """SELECT
                      (SELECT COUNT(*) FROM perp_forward_audit
                        WHERE asset=? AND horizon_minutes=? AND issued_at>=?)
                          AS incumbent_issued,
                      (SELECT COUNT(*) FROM rival_model_shadow_forecasts
                        WHERE asset=? AND horizon_minutes=?
                          AND family='regime_v1' AND issued_at>=?)
                          AS v1_issued""",
                (asset, int(horizon), since, asset, int(horizon), since),
            ).fetchone()
            incumbent_issued = int(counts["incumbent_issued"] or 0)
            v1_issued = int(counts["v1_issued"] or 0)
            split = conn.execute(
                """SELECT AVG(CASE WHEN issued_at>=? THEN incumbent_brier END)
                          - AVG(CASE WHEN issued_at>=? THEN model_brier END)
                          AS gain_7d,
                          AVG(CASE WHEN issued_at<? THEN incumbent_brier END)
                          - AVG(CASE WHEN issued_at<? THEN model_brier END)
                          AS gain_8_30
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family=?
                     AND issued_at>=? AND settled_at IS NOT NULL
                     AND outcome_up IS NOT NULL
                     AND probability_up NOT IN (0,1)""",
                ((stamp - timedelta(days=7)).isoformat(),
                 (stamp - timedelta(days=7)).isoformat(),
                 (stamp - timedelta(days=7)).isoformat(),
                 (stamp - timedelta(days=7)).isoformat(),
                 asset, int(horizon), family, since),
            ).fetchone()
    except Exception:  # noqa: BLE001 - display only, never raise
        return {"issued": 0}
    drift = _weight_drift(store, asset, horizon, family=family)
    return {
        "issued": issued,
        "graded": int(row["graded"] or 0),
        "decided": int(row["decided"] or 0),
        "hits": int(row["hits"] or 0),
        "windows": int(row["windows"] or 0),
        "model_brier": model_brier,
        "incumbent_brier": incumbent_brier,
        "baseline_brier": row["baseline_brier"],
        "first_issued": row["first_issued"],
        "paired_gain": paired_gain,
        "paired_gain_vs_regime_v1": paired_gain_vs_regime_v1,
        "selection_effect": selection_effect,
        "coverage": (issued / incumbent_issued) if incumbent_issued else None,
        "availability_ratio": (issued / v1_issued) if v1_issued else None,
        "updates": drift["updates"],
        "weight_drift_mean": drift["weight_drift_mean"],
        "weight_drift_max": drift["weight_drift_max"],
        "paired_gain_last_7d": (
            None if split is None or split["gain_7d"] is None
            else float(split["gain_7d"])
        ),
        "paired_gain_days_8_30": (
            None if split is None or split["gain_8_30"] is None
            else float(split["gain_8_30"])
        ),
    }
