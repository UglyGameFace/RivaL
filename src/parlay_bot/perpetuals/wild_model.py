"""regime_wild_v1 challenger (audit Step 5, 2026-11-10).

The main model vs coin flip is level overall but positive only in wild
markets (audit evidence: calm -0.0014, normal -0.0016, wild +0.0158 at
5m; wild +0.0347 at 15m). This family tests whether a wild-only caller
earns a track record. It has NO edge until graded: it starts with no
track record and must earn one through the existing Route B gates
(150 targets + 21 days), like every other challenger.

Design (simplest faithful one, mirroring regime_v1): the family reuses
the incumbent's probability verbatim -- the only thing new is the
gate. A wild call IS the incumbent's call, issued as a shadow forecast
only when the market-condition stamp already recorded at issuance says
wild. There is no offline artifact to pin (no D14 pin): selected()
verifies continuity of the live weights the gate last served, exactly
like regime_v1.

The "wild" condition is NOT a new volatility measure. It is the
existing stamp: ``issue_regime(features)`` in regime_evidence.py
classifies the 5-minute log-return volatility in pct (features[2]) as
quiet < 0.10, normal < 0.35, turbulent >= 0.35, and the audit's name
table maps turbulent -> wild (answer_cards.py, research_dashboard.py).
Wild therefore means ``regime == "turbulent"``, i.e. features[2] >=
0.35, stamped at issuance in perp_forward_regimes alongside the
incumbent forecast. The family never computes its own volatility.

Never raises: an unavailable challenger is an empty list / None / an
empty report, never a failed incumbent cycle. Never appears in the
OTG display: "regime_" is a CHALLENGER_PREFIXES prefix, so live_lean()
returns None for this family, and it is unpromoted until Route B
promotes it.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fusion import utc

FAMILY = "regime_wild_v1"
GATE_METHOD = "issue-regime-turbulent-v1"

# Feature contract the gate serves. regime_wild_v1 has no trained
# artifact of its own: the digest pins the gate definition plus the
# live decider it gates, so a MODEL bump deliberately breaks continuity
# (and the pinning test) instead of silently serving a different model
# under the same name.
_CONTRACT = (
    FAMILY,
    "issue-regime-turbulent",
    "vol>=0.35",
    "prospective-logistic-persistent-v3",
    "10-features",
)
FEATURE_DIGEST = hashlib.sha256("|".join(_CONTRACT).encode()).hexdigest()

# The trial window the Performance page reports over (same as regime_v1
# and the pooled row).
TRIAL_DAYS = 7

# The market-condition stamp value that counts as wild. This is the
# "turbulent" stamp from issue_regime() (regime_evidence.py: the
# 5-minute log-return volatility in pct, features[2], >= 0.35); the
# audit's name table (answer_cards.py) displays it as "wild".
WILD_REGIME = "turbulent"


def wild_calls(*, regime, probability, training_rows, model_sha256):
    """One shadow call reusing the incumbent's probability, or none.

    Returns [] when the issuance stamp is not wild (quiet or normal),
    when the incumbent's probability is not a strict 0..1 probability,
    or when the weights hash is not a 64-hex digest. Never raises.
    """
    try:
        if str(regime) != WILD_REGIME:
            return []
        p = float(probability)
        if not 0 < p < 1:
            return []
        sha = str(model_sha256)
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            return []
        return [
            {
                "family": FAMILY,
                "probability": p,
                "training_rows": int(training_rows),
                "model_sha256": sha,
            }
        ]
    except (TypeError, ValueError, OverflowError):
        return []


def calibration_status(store, asset: str, horizon: int) -> dict:
    """Calibration record for receipts; the wild family has no Platt fit.

    Like regime_v1 it reuses the incumbent's probability verbatim, so
    there is no calibration artifact to report. Route B's calibration
    screen is the evidence for this family, not a stored calibration.
    """
    return {"method": "none", "status": "unevaluated"}


def artifact_identity(store, asset, horizon) -> dict | None:
    """Continuity identity for (asset, horizon), or None.

    No offline artifact exists for regime_wild_v1, so the identity is
    the live weights the gate last served: their sha, the last issuance,
    and how many settled wild rows exist. selected() checks the feature
    digest matches, trained-through advanced, and the graded count did
    not go backwards -- the same continuity check as regime_v1.
    """
    try:
        with store.connect() as conn:
            latest = conn.execute(
                """SELECT training_sha256, issued_at
                   FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                     AND settled_at IS NOT NULL AND outcome_up IN (0,1)
                   ORDER BY issued_at DESC LIMIT 1""",
                (FAMILY, asset, int(horizon)),
            ).fetchone()
            if latest is None:
                return None
            row = conn.execute(
                """SELECT COUNT(*) AS n
                   FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                     AND settled_at IS NOT NULL AND outcome_up IN (0,1)""",
                (FAMILY, asset, int(horizon)),
            ).fetchone()
    except Exception:  # noqa: BLE001 - an unavailable challenger is None
        return None
    return {
        "feature_digest": FEATURE_DIGEST,
        "weights_sha256": str(latest["training_sha256"]),
        "trained_through": str(latest["issued_at"]),
        "graded_count": int(row["n"]) if row is not None else 0,
    }


def trial_report(store, *, asset, horizon, days=TRIAL_DAYS, now=None) -> dict:
    """D5-style trial report for regime_wild_v1 over the last `days` days.

    issued / graded / decided / hits / windows / model_brier /
    incumbent_brier / baseline_brier are the same columns as the pooled
    trial row, measured on regime_wild_v1's issued subset only. The
    decomposition is the extra part:
      paired_gain      = incumbent Brier - wild Brier on the subset
                         (positive: wild beat the incumbent on the
                         targets it actually issued on);
      selection_effect = incumbent Brier on the subset - incumbent
                         Brier on ALL targets in the same window
                         (positive: the subset was harder than average
                         for the incumbent; negative: easier).
    coverage = issued wild rows / issued incumbent rows in the window.
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
                (asset, int(horizon), FAMILY, since),
            ).fetchone()
            incumbent = conn.execute(
                """SELECT COUNT(*) AS issued,
                          AVG(model_brier) AS model_brier
                   FROM perp_forward_audit
                   WHERE asset=? AND horizon_minutes=? AND model_brier IS NOT NULL
                     AND issued_at>=?""",
                (asset, int(horizon), since),
            ).fetchone()
    except Exception:  # noqa: BLE001 - a failed read is an empty report
        return {"issued": 0}
    issued = int(row["issued"] or 0)
    if issued == 0:
        return {"issued": 0}
    model_brier = row["model_brier"]
    incumbent_brier = row["incumbent_brier"]
    all_incumbent_brier = incumbent["model_brier"]
    paired_gain = (
        None
        if model_brier is None or incumbent_brier is None
        else float(incumbent_brier) - float(model_brier)
    )
    selection_effect = (
        None
        if incumbent_brier is None or all_incumbent_brier is None
        else float(incumbent_brier) - float(all_incumbent_brier)
    )
    incumbent_issued = int(incumbent["issued"] or 0)
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
        "selection_effect": selection_effect,
        "coverage": (issued / incumbent_issued) if incumbent_issued else None,
    }
