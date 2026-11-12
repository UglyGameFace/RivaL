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
    for horizon in (5, 15, 60):
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
        })
    return results


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
            try:
                with store.connect() as conn:
                    # Uses the existing asset + model_version + issuance index.
                    exists = conn.execute(
                        "SELECT to_regclass(?) AS tab", ("perp_forward_audit",)
                    ).fetchone()["tab"]
                    if not exists:
                        return {"status": "unavailable",
                                "reason": "table_not_initialized", "scopes": []}
                    result = conn.execute(
                        """SELECT venue,contract,quote,horizon_minutes,issued_at,
                                  target_opened_at,model_brier,baseline_brier
                           FROM perp_forward_audit
                           WHERE asset=? AND model_version=?
                             AND issued_at>=? AND issued_at<=?
                             AND settled_at IS NOT NULL
                             AND outcome_up IN (0,1)
                             AND model_brier BETWEEN 0 AND 1
                             AND baseline_brier BETWEEN 0 AND 1
                             AND issued_at < target_opened_at
                           ORDER BY issued_at DESC LIMIT 500""",
                        (asset, MODEL, (now - timedelta(hours=24)).isoformat(),
                         now.isoformat()),
                    ).fetchall()
                scopes.extend(summarize_rows(asset, [dict(x) for x in result]))
            except Exception:  # noqa: BLE001 - optional isolated read must fail closed
                failures += 1
        return {
            "status": "observed" if scopes and not failures else
                      "partial" if scopes else "unavailable" if failures else "no_pairs",
            "source": "postgres_perp_forward_audit",
            "captured_at": now.isoformat(),
            "window_hours": 24, "sample_limit_per_asset": 500,
            "failed_assets": failures, "scopes": scopes,
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
