"""Bounded, read-only incumbent Brier evidence for RivaL Evidence Board.

Reads already-settled, causal, paired forecast/baseline rows. No schema
initialization, backfills, writes, training, predictions, or new network calls.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PAIRED_SCORE_RECEIPT="
MODEL = "prospective-logistic-persistent-v3"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)
SAMPLE_LIMIT_PER_SCOPE = 500
SAMPLING_POLICY = "per-asset-horizon-v2"


def summarize_rows(asset: str, rows: list[dict]) -> list[dict]:
    """Source-paired observations, deduplicated by exact contract and target."""
    if asset not in ASSETS:
        raise ValueError("unsupported asset")
    unique = {}
    for row in rows:
        try:
            horizon = row["horizon_minutes"]
            key = (row["venue"], row["contract"], row["quote"],
                   horizon, row["target_opened_at"])
            model = float(row["model_brier"])
            baseline = float(row["baseline_brier"])
            issued = str(row["issued_at"])
            target = str(row["target_opened_at"])
            if (type(horizon) is not int or horizon not in (5, 15, 60)
                    or not issued < target
                    or not (math.isfinite(model) and math.isfinite(baseline))
                    or not (0 <= model <= 1 and 0 <= baseline <= 1)):
                continue
            if key not in unique or issued < unique[key][0]:
                unique[key] = (issued, model, baseline)
        except (KeyError, TypeError, ValueError):
            continue
    scores = defaultdict(list)
    for key, (issued, model, baseline) in unique.items():
        scores[key[3]].append((model, baseline))
    results = []
    for horizon in HORIZONS:
        pairs = scores.get(horizon, [])
        if not pairs:
            continue
        n = len(pairs)
        results.append({
            "asset": asset, "horizon": horizon, "paired_outcomes": n,
            "model_brier": round(sum(x[0] for x in pairs) / n, 6),
            "baseline_brier": round(sum(x[1] for x in pairs) / n, 6),
            "sample_bounded": True, "after_cost_qualified": False,
            "model_version": MODEL,
            "sampling_policy": SAMPLING_POLICY,
            "unique_target_windows": len({key[4] for key in unique if key[3] == horizon}),
            "venue_count": len({key[0] for key in unique if key[3] == horizon}),
        })
    return results


def _scope_rows(conn, asset: str, horizon: int, now: datetime) -> list[dict]:
    """Bound each asset/horizon separately; busy horizons cannot hide others.

    The asset/horizon/issuance index bounds this read. Venue observations
    remain descriptive source pairs, not independent trades or outcomes.
    """
    if asset not in ASSETS or horizon not in HORIZONS:
        raise ValueError("unsupported scoring scope")
    result = conn.execute(
        """SELECT venue,contract,quote,horizon_minutes,issued_at,
                  target_opened_at,model_brier,baseline_brier
           FROM perp_forward_audit
           WHERE asset=? AND horizon_minutes=? AND model_version=?
             AND issued_at>=? AND issued_at<=?
             AND settled_at IS NOT NULL AND settled_at<=?
             AND outcome_up IN (0,1)
             AND model_brier BETWEEN 0 AND 1
             AND baseline_brier BETWEEN 0 AND 1
             AND issued_at < target_opened_at
           ORDER BY issued_at DESC,venue,contract,quote,target_opened_at
           LIMIT ?""",
        (asset, horizon, MODEL, (now - timedelta(hours=24)).isoformat(),
         now.isoformat(), now.isoformat(), SAMPLE_LIMIT_PER_SCOPE),
    ).fetchall()
    return [dict(row) for row in result]


def read_scores() -> dict:
    """Never call any initialize()/create method on the production database."""
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    if (settings.rival_storage_backend != "postgres"
            or settings.rival_database_url is None):
        return {"status": "unavailable", "reason": "postgres_unavailable",
                "scopes": []}
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    now = datetime.now(UTC)
    scopes = []
    failures = 0
    try:
        for asset in ASSETS:
            for horizon in HORIZONS:
                try:
                    with store.connect() as conn:
                        exists = conn.execute(
                            "SELECT to_regclass(?) AS tab", ("perp_forward_audit",)
                        ).fetchone()["tab"]
                        if not exists:
                            return {"status": "unavailable",
                                    "reason": "table_not_initialized", "scopes": []}
                        rows = _scope_rows(conn, asset, horizon, now)
                    scopes.extend(summarize_rows(asset, rows))
                except Exception:  # noqa: BLE001 - isolate each asset/horizon read
                    failures += 1
        return {
            "status": "observed" if scopes and not failures else
                      "partial" if scopes else "unavailable" if failures else "no_pairs",
            "source": "postgres_perp_forward_audit",
            "captured_at": now.isoformat(),
            "window_hours": 24, "sample_limit_per_scope": SAMPLE_LIMIT_PER_SCOPE,
            "expected_scopes": len(ASSETS) * len(HORIZONS),
            "failed_scopes": failures, "scopes": scopes,
            "sample_unit": "venue_contract_target",
            "sampling_policy": SAMPLING_POLICY,
            "proper_scoring": "brier_lower_is_better",
            "after_cost_qualified": False,
            "no_statistical_superiority_claim": True,
        }
    finally:
        store.close()


def main() -> int:
    try:
        report = read_scores()
    except Exception:  # noqa: BLE001
        report = {"status": "unavailable", "scopes": []}
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
