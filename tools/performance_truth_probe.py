"""TEMP read-only probe (do not merge): overall status check."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PERFORMANCE_PROBE="


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals import host_memory
    from parlay_bot.storage.hot import PostgresHotStore

    store = PostgresHotStore(Settings().rival_database_url.get_secret_value(), read_only=True)
    now = datetime.now(UTC)
    out: dict = {"now": now.isoformat()}
    try:
        with store.connect() as conn:
            q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a).fetchall()]  # noqa: E731
            out["reviews"] = q("""SELECT COUNT(*) AS n, MAX(issued_at) AS last FROM rival_intelligence_calls
                                  WHERE issued_at>=?""", "2026-11-05T14:00:00+00:00")
            out["hyp"] = q("""SELECT status, COUNT(*) AS n FROM rival_llm_feature_hypotheses
                              WHERE attempted_at>=? GROUP BY status""", "2026-11-05T13:44:00+00:00")
            out["incidents"] = q("SELECT issue_key, detail FROM rival_runtime_health_incidents WHERE state='OPEN'")
            out["memory"] = q("""SELECT hour, level, text FROM rival_problem_log
                                 WHERE text LIKE '%memory receipt%' OR text LIKE '%high host RSS%'
                                 ORDER BY last_at DESC LIMIT 14""")
            out["problems"] = q("""SELECT level, SUM(count) AS n, MAX(last_at) AS last, MAX(text) AS text
                                   FROM rival_problem_log WHERE last_at>=?
                                   GROUP BY level, template ORDER BY n DESC LIMIT 15""",
                                (now - timedelta(minutes=45)).isoformat())
            out["cycles"] = q("""SELECT completed_at, math_ok, model_review_ok FROM perp_learning_cycle_health
                                 ORDER BY completed_at DESC LIMIT 3""")
            out["liquidity"] = q("""SELECT venue, bucket_start, SUM(book_samples) AS books,
                                     SUM(buy_trades+sell_trades) AS trades, COUNT(*) AS assets
                                  FROM perp_liquidity_5m WHERE bucket_start>=?
                                  GROUP BY venue, bucket_start ORDER BY bucket_start DESC, venue LIMIT 40""",
                                 (now - timedelta(hours=3)).isoformat())
            out["stream_errors"] = q("""SELECT hour, SUM(count) AS n, MAX(text) AS text FROM rival_problem_log
                                        WHERE hour>=? AND (text LIKE '%stream%' OR text LIKE '%stale%'
                                          OR text LIKE '%Hyperliquid%' OR text LIKE '%hyperliquid%')
                                        GROUP BY hour, template ORDER BY hour DESC LIMIT 20""",
                                     (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:00:00+00:00"))
            out["boots"] = q("SELECT started_at FROM rival_runtime_health_boots ORDER BY started_at DESC LIMIT 2")
    finally:
        store.close()
    out["container"] = host_memory.snapshot().detail
    try:
        status = open("/proc/1/status").read()
        out["bot_rss_mb"] = next(int(l.split()[1]) // 1024 for l in status.splitlines() if l.startswith("VmRSS"))
    except OSError:
        pass
    return out


def main() -> int:
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001
        report = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    print(MARKER + json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
