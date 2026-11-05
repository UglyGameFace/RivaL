"""TEMP read-only probe (do not merge): prediction health, per model family.

Runs on the Discloud host through performance-truth-audit.yml, using the
bot's read-only PostgreSQL connection. Aggregates only; no writes, no rows,
no secrets. Every query is bounded to the last 7 days.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PERFORMANCE_PROBE="


def _rows(conn, sql, args=()):
    return [{k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
             for k, v in dict(r).items()} for r in conn.execute(sql, args).fetchall()]


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.forward_audit import MODEL
    from parlay_bot.storage.hot import PostgresHotStore

    store = PostgresHotStore(Settings().rival_database_url.get_secret_value(), read_only=True)
    now = datetime.now(UTC)
    week, day, day3 = ((now - timedelta(days=d)).isoformat() for d in (7, 1, 3))
    out: dict = {"now": now.isoformat(), "model": MODEL, "timings": {}}

    def q(name, sql, args=()):
        started = time.monotonic()
        try:
            with store.connect() as conn:
                out[name] = _rows(conn, sql, args)
        except Exception as exc:  # noqa: BLE001
            out[name + "_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        out["timings"][name] = round(time.monotonic() - started, 2)

    try:
        # 1. Incumbent by venue and horizon (7 days, graded only).
        q("incumbent_7d", """
            SELECT venue, horizon_minutes AS h, COUNT(*) AS n,
                   AVG(model_brier) AS brier, AVG(baseline_brier) AS base_brier,
                   AVG(outcome_up) AS up_rate, AVG(ABS(probability_up-0.5)) AS conf,
                   AVG(CASE WHEN (probability_up>0.5)=(outcome_up=1) THEN 1.0 ELSE 0.0 END) AS hit
            FROM perp_forward_audit
            WHERE model_version=? AND issued_at>=? AND outcome_up IN (0,1)
              AND model_brier IS NOT NULL AND baseline_brier IS NOT NULL
            GROUP BY venue, horizon_minutes ORDER BY h, venue""", (MODEL, week))
        # 2. Incumbent daily trend (all venues).
        q("incumbent_daily", """
            SELECT SUBSTR(issued_at,1,10) AS day, horizon_minutes AS h, COUNT(*) AS n,
                   AVG(model_brier) AS brier, AVG(baseline_brier) AS base_brier
            FROM perp_forward_audit
            WHERE model_version=? AND issued_at>=? AND outcome_up IN (0,1)
              AND model_brier IS NOT NULL AND baseline_brier IS NOT NULL
            GROUP BY SUBSTR(issued_at,1,10), horizon_minutes ORDER BY day, h""", (MODEL, week))
        # 3. Every shadow family vs incumbent and up-rate, by venue and horizon.
        q("families_7d", """
            SELECT family, venue, horizon_minutes AS h, COUNT(*) AS n,
                   AVG(model_brier) AS brier, AVG(incumbent_brier) AS inc_brier,
                   AVG(baseline_brier) AS base_brier, AVG(outcome_up) AS up_rate,
                   AVG(probability_up) AS mean_p, AVG(ABS(probability_up-0.5)) AS conf,
                   MIN(issued_at) AS first_issued
            FROM rival_model_shadow_forecasts
            WHERE issued_at>=? AND settled_at IS NOT NULL AND outcome_up IN (0,1)
              AND model_brier IS NOT NULL
            GROUP BY family, venue, horizon_minutes HAVING COUNT(*)>=30
            ORDER BY family, h, venue""", (week,))
        # 4. Calibration (reliability) of the incumbent and the flow models.
        q("calibration", """
            SELECT family, horizon_minutes AS h,
                   CAST(probability_up*10 AS INTEGER) AS bin, COUNT(*) AS n,
                   AVG(probability_up) AS mean_p, AVG(outcome_up) AS up_rate
            FROM rival_model_shadow_forecasts
            WHERE issued_at>=? AND settled_at IS NOT NULL AND outcome_up IN (0,1)
              AND (family IN ('flow_v1','flow_v2','knn21_v1','gaussian_nb_v1')
                   OR family LIKE 'calib_%')
            GROUP BY family, horizon_minutes, CAST(probability_up*10 AS INTEGER)
            ORDER BY family, h, bin""", (week,))
        q("incumbent_calibration", """
            SELECT horizon_minutes AS h, CAST(probability_up*10 AS INTEGER) AS bin,
                   COUNT(*) AS n, AVG(probability_up) AS mean_p, AVG(outcome_up) AS up_rate
            FROM perp_forward_audit
            WHERE model_version=? AND issued_at>=? AND outcome_up IN (0,1)
            GROUP BY horizon_minutes, CAST(probability_up*10 AS INTEGER)
            ORDER BY h, bin""", (MODEL, week))
        # 5. Six-year models: each trained version, 3 days, vs up-rate.
        q("pooled_by_model_3d", """
            SELECT family, SUBSTR(training_sha256,1,12) AS model, horizon_minutes AS h,
                   COUNT(*) AS n, AVG(model_brier) AS brier, AVG(baseline_brier) AS base_brier,
                   AVG(incumbent_brier) AS inc_brier, MIN(issued_at) AS first, MAX(issued_at) AS last
            FROM rival_model_shadow_forecasts
            WHERE family LIKE 'pooled%' AND issued_at>=? AND settled_at IS NOT NULL
              AND model_brier IS NOT NULL
            GROUP BY family, SUBSTR(training_sha256,1,12), horizon_minutes
            ORDER BY h, first""", (day3,))
        q("pooled_imported", """
            SELECT family, horizon_minutes AS h, trained_through, gates_passed,
                   imported_at, SUBSTR(model_sha256,1,12) AS model
            FROM rival_pooled_models ORDER BY imported_at DESC LIMIT 20""")
        # 6. Shadow settlement queue by why rows are unsettled.
        q("shadow_unsettled", """
            SELECT CASE WHEN f.venue IS NULL THEN 'no_incumbent'
                        WHEN f.settled_at IS NULL THEN 'incumbent_pending'
                        WHEN f.outcome_up IN (0,1) THEN 'settleable'
                        ELSE 'withdrawn' END AS state,
                   COUNT(*) AS n, MIN(s.target_opened_at) AS oldest
            FROM rival_model_shadow_forecasts s
            LEFT JOIN perp_forward_audit f
              ON f.model_version=? AND f.venue=s.venue AND f.contract=s.contract
             AND f.quote=s.quote AND f.horizon_minutes=s.horizon_minutes
             AND f.reference_opened_at=s.reference_opened_at
            WHERE s.settled_at IS NULL GROUP BY 1""", (MODEL,))
        # 7. Issued vs settled shadow rows per hour (throughput), last 24 h.
        q("shadow_hourly_24h", """
            SELECT SUBSTR(issued_at,1,13) AS hour, COUNT(*) AS issued,
                   SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled
            FROM rival_model_shadow_forecasts WHERE issued_at>=?
            GROUP BY SUBSTR(issued_at,1,13) ORDER BY hour""", (day,))
        # 8. Intelligence stances and how the followed calls did.
        q("intelligence_7d", """
            SELECT c.horizon_minutes AS h, c.stance, c.direction, COUNT(*) AS n,
                   AVG(CASE WHEN (f.probability_up>0.5 AND f.outcome_up=1)
                             OR (f.probability_up<0.5 AND f.outcome_up=0)
                        THEN 1.0 ELSE 0.0 END) AS model_hit
            FROM rival_intelligence_calls c
            JOIN perp_forward_audit f ON f.asset=c.asset AND f.horizon_minutes=c.horizon_minutes
             AND f.target_opened_at=c.target_opened_at AND f.model_version=?
             AND f.outcome_up IN (0,1)
            WHERE c.issued_at>=?
            GROUP BY c.horizon_minutes, c.stance, c.direction ORDER BY h""", (MODEL, week))
        # 9. Latest cycle diagnostics (why streams were skipped).
        q("cycles_24h", """
            SELECT COUNT(*) AS cycles, SUM(issued_forecasts) AS issued,
                   SUM(target_expired) AS expired, SUM(duplicate_forecasts) AS duplicates,
                   SUM(stale_last_candle) AS stale, SUM(insufficient_training) AS insufficient,
                   SUM(invalid_data) AS invalid
            FROM perp_forward_cycle_diagnostics WHERE checked_at>=?""", (day,))
    finally:
        store.close()
    return out


def main() -> int:
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001 - never print DSN or SQL
        report = {"error": type(exc).__name__}
    print(MARKER + json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
