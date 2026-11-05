"""TEMP read-only probe (do not merge): RivaL Intelligence audit + slow settle plan.

Runs on the Discloud host via performance-truth-audit.yml with the bot's
read-only PostgreSQL connection. Aggregates and query plans only.
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
    out: dict = {"now": now.isoformat(), "timings": {}}

    def q(name, sql, args=()):
        started = time.monotonic()
        try:
            with store.connect() as conn:
                out[name] = _rows(conn, sql, args)
        except Exception as exc:  # noqa: BLE001
            out[name + "_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out["timings"][name] = round(time.monotonic() - started, 2)

    try:
        q("intel_daily", """
            SELECT SUBSTR(issued_at,1,10) AS day, model, stance, direction, COUNT(*) AS n
            FROM rival_intelligence_calls WHERE issued_at>=?
            GROUP BY 1,2,3,4 ORDER BY 1,2,3,4""", (week,))
        q("intel_latest", """
            SELECT asset, MAX(issued_at) AS last, COUNT(*) AS n
            FROM rival_intelligence_calls GROUP BY asset""")
        # Timing: seconds after the target slot opened (record() drops >= 300 s).
        q("intel_issue_offset", """
            SELECT horizon_minutes AS h, issued_at, target_opened_at
            FROM rival_intelligence_calls WHERE issued_at>=? ORDER BY issued_at DESC LIMIT 600""", (day,))
        # Graded per target: one consensus outcome per (asset, horizon, target).
        q("intel_graded", """
            WITH t AS (
              SELECT f.asset, f.horizon_minutes AS h, f.target_opened_at,
                     AVG(f.outcome_up) AS up, AVG(f.probability_up) AS p, COUNT(*) AS venues
              FROM perp_forward_audit f
              WHERE f.model_version=? AND f.issued_at>=? AND f.outcome_up IN (0,1)
              GROUP BY f.asset, f.horizon_minutes, f.target_opened_at)
            SELECT c.horizon_minutes AS h, c.stance, c.direction, COUNT(*) AS n,
                   AVG(CASE WHEN c.direction='up' AND t.up>0.5 THEN 1.0
                            WHEN c.direction='down' AND t.up<0.5 THEN 1.0
                            WHEN c.direction IN ('up','down') THEN 0.0 END) AS direction_right,
                   AVG(CASE WHEN (t.p>0.5)=(t.up>0.5) THEN 1.0 ELSE 0.0 END) AS consensus_right,
                   AVG(c.consensus_up) AS mean_consensus
            FROM rival_intelligence_calls c
            JOIN t ON t.asset=c.asset AND t.h=c.horizon_minutes AND t.target_opened_at=c.target_opened_at
            WHERE c.issued_at>=? AND t.up<>0.5
            GROUP BY c.horizon_minutes, c.stance, c.direction ORDER BY h""", (MODEL, week, week))
        q("hypotheses", """
            SELECT SUBSTR(attempted_at,1,10) AS day, status, model, COUNT(*) AS n,
                   MAX(attempt_count) AS max_attempts
            FROM rival_llm_feature_hypotheses WHERE attempted_at>=?
            GROUP BY 1,2,3 ORDER BY 1,2""", (week,))
        q("hypotheses_proposed", """
            SELECT asset, slot_utc, hypothesis, feature_indices_json, model
            FROM rival_llm_feature_hypotheses WHERE status='proposed'
            ORDER BY slot_utc DESC LIMIT 20""")
        q("ai_research", """
            SELECT SUBSTR(issued_at,1,10) AS day, model, direction, confidence, COUNT(*) AS n
            FROM perp_ai_research WHERE issued_at>=? GROUP BY 1,2,3,4 ORDER BY 1""", (week,))
        q("problem_intel_7d", """
            SELECT logger, level, template, SUM(count) AS n, MAX(last_at) AS last, MAX(text) AS text
            FROM rival_problem_log
            WHERE hour>=? AND (logger LIKE '%intellig%' OR logger LIKE '%local_first%'
                  OR logger LIKE '%hypothes%' OR template LIKE '%Intelligence%'
                  OR template LIKE '%LFM%' OR template LIKE '%hypothes%')
            GROUP BY logger, level, template ORDER BY n DESC LIMIT 30""",
          ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:00:00+00:00"),))
        # Slow settle SELECT: the exact statement, planned and run (SELECT only).
        q("settle_plan", """
            EXPLAIN (ANALYZE, BUFFERS)
            SELECT s.*,f.outcome_up AS verified_outcome_up,
                   f.settled_at AS verified_settled_at
            FROM rival_model_shadow_forecasts s
            JOIN perp_forward_audit f
             ON f.model_version='prospective-logistic-persistent-v3'
             AND f.venue=s.venue AND f.contract=s.contract
             AND f.quote=s.quote AND f.asset=s.asset
             AND f.horizon_minutes=s.horizon_minutes
             AND f.reference_opened_at=s.reference_opened_at
             AND f.issued_at=s.issued_at
            WHERE s.settled_at IS NULL AND f.settled_at IS NOT NULL
             AND f.settled_at<=? AND f.outcome_up IN (0,1)
             AND s.target_opened_at=f.target_opened_at AND s.target_opened_at>=?
            ORDER BY s.target_opened_at LIMIT 240""", (now.isoformat(), day3))
        q("shadow_table_stats", """
            SELECT relname, n_live_tup, n_dead_tup, seq_scan, idx_scan,
                   last_autovacuum, last_autoanalyze,
                   pg_size_pretty(pg_total_relation_size(relid)) AS size
            FROM pg_stat_user_tables
            WHERE relname IN ('rival_model_shadow_forecasts','perp_forward_audit')""")
        q("shadow_indexes", """
            SELECT s.indexrelname AS idx, s.idx_scan, pg_size_pretty(pg_relation_size(s.indexrelid)) AS size,
                   pg_get_indexdef(s.indexrelid) AS def
            FROM pg_stat_user_indexes s
            WHERE s.relname IN ('rival_model_shadow_forecasts','perp_forward_audit')""")
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
