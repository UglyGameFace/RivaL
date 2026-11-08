"""regime_v1 challenger (spec rival-regime-gated-spec-20261107).

D3: v1 reuses the live logistic's weights verbatim -- the only thing new
is the gate. A regime call IS the incumbent's call, issued as a shadow
forecast only when the regime gate passes. There is no offline artifact
to pin (no D14 pin): selected() verifies continuity of the live weights
the gate last served, exactly like flow_v3/liq_v1.

Never raises: an unavailable challenger is an empty list / None / an
empty report, never a failed incumbent cycle.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.regime_gate import trending_gate

FAMILY = "regime_v1"
GATE_METHOD = "efficiency-ratio-20-v1"

# Feature contract the gate serves. regime_v1 has no trained artifact of
# its own: the digest pins the gate definition plus the live decider it
# gates, so a MODEL bump deliberately breaks continuity (and the pinning
# test) instead of silently serving a different model under the same name.
_CONTRACT = (
    FAMILY,
    "efficiency-ratio-20",
    "er>0.30",
    "prospective-logistic-persistent-v3",
    "10-features",
)
FEATURE_DIGEST = hashlib.sha256("|".join(_CONTRACT).encode()).hexdigest()

# The trial window the Performance page reports over (same as pooled).
TRIAL_DAYS = 7


def regime_calls(*, candles, probability, training_rows, model_sha256):
    """One shadow call reusing the incumbent's probability, or none.

    Returns [] when the gate abstains (not trending), when the
    incumbent's probability is not a strict 0..1 probability, or when
    the weights hash is not a 64-hex digest. Never raises.
    """
    try:
        if not trending_gate(candles):
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


def artifact_identity(store, asset, horizon) -> dict | None:
    """Continuity identity for (asset, horizon), or None.

    No offline artifact exists for regime_v1, so the identity is the
    live weights the gate last served: their sha, the last issuance, and
    how many settled regime rows exist. selected() checks the feature
    digest matches, trained-through advanced, and the graded count did
    not go backwards -- the same continuity check as flow_v3/liq_v1.
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
    """D5 trial report for regime_v1 over the last `days` days.

    issued / graded / decided / hits / windows / model_brier /
    incumbent_brier / baseline_brier are the same columns as the pooled
    trial row, measured on regime_v1's issued subset only. The
    decomposition is the extra part:
      paired_gain      = incumbent Brier - regime Brier on the subset
                         (positive: regime beat the incumbent on the
                         targets it actually issued on);
      selection_effect = incumbent Brier on the subset - incumbent
                         Brier on ALL targets in the same window
                         (positive: the subset was harder than average
                         for the incumbent; negative: easier).
    coverage = issued regime rows / issued incumbent rows in the window.
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
