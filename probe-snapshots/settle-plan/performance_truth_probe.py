"""TEMP read-only probe (do not merge): problem log since the last release."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime

MARKER = "RIVAL_PERFORMANCE_PROBE="


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore

    store = PostgresHotStore(Settings().rival_database_url.get_secret_value(), read_only=True)
    since = os.environ.get("RIVAL_PROBE_SINCE") or "2026-11-05T06:12:00+00:00"
    out: dict = {"now": datetime.now(UTC).isoformat(), "since": since}
    try:
        with store.connect() as conn:
            out["problems"] = [dict(r) for r in conn.execute(
                """SELECT level, template, SUM(count) AS n, MAX(last_at) AS last, MAX(text) AS text
                   FROM rival_problem_log WHERE last_at>=?
                   GROUP BY level, template ORDER BY n DESC LIMIT 40""", (since,)).fetchall()]
            out["incidents"] = [dict(r) for r in conn.execute(
                """SELECT issue_key, state, detail, last_seen_at, occurrence_count
                   FROM rival_runtime_health_incidents WHERE last_seen_at>=?
                   ORDER BY last_seen_at DESC LIMIT 30""", (since,)).fetchall()]
            out["index"] = [dict(r) for r in conn.execute(
                """SELECT indexrelname, idx_scan FROM pg_stat_user_indexes
                   WHERE indexrelname='idx_rival_model_shadow_venue_issued'""").fetchall()]
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
