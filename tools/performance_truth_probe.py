"""Read-only production probe: are the Bot Performance numbers true?

Runs on the Discloud host through the console API (see the workflow
performance-truth-audit.yml). Prints one JSON line of aggregate counts and
timings. No writes, no secrets, no row contents.
"""
from __future__ import annotations

import itertools
import json
import time
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PERFORMANCE_PROBE="


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return {k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
            for k, v in dict(row).items()} if row else None


def _timed(fn):
    start = time.monotonic()
    try:
        result = fn()
        error = None
    except Exception as exc:  # noqa: BLE001
        result, error = None, type(exc).__name__
    return round(time.monotonic() - start, 2), result, error


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    now = datetime.now(UTC)
    day, week = (now - timedelta(days=1)).isoformat(), (now - timedelta(days=7)).isoformat()
    out: dict = {"now": now.isoformat(), "model": MODEL}
    try:
        with store.connect() as conn:
            out["cycle_24h"] = _one(conn, """
                SELECT COUNT(*) AS cycles, MIN(checked_at) AS first, MAX(checked_at) AS last,
                       SUM(issued_forecasts) AS issued, SUM(settled_forecasts) AS settled,
                       SUM(target_expired) AS expired, SUM(duplicate_forecasts) AS duplicates,
                       SUM(stale_last_candle) AS stale, SUM(short_history) AS short_history,
                       SUM(insufficient_training) AS insufficient_training,
                       SUM(invalid_data) AS invalid, MAX(selected_streams) AS streams
                FROM perp_forward_cycle_diagnostics WHERE checked_at>=?""", (day,))
            stamps = [r["checked_at"] for r in conn.execute(
                "SELECT checked_at FROM perp_forward_cycle_diagnostics "
                "WHERE checked_at>=? ORDER BY checked_at", (day,)).fetchall()]
            gaps = []
            for a, b in itertools.pairwise(stamps):
                gaps.append((datetime.fromisoformat(str(b)) -
                             datetime.fromisoformat(str(a))).total_seconds() / 60)
            if gaps:
                gaps.sort()
                out["cycle_gap_minutes"] = {
                    "median": round(gaps[len(gaps) // 2], 1), "max": round(gaps[-1], 1),
                    "over_6": sum(g > 6 for g in gaps), "over_11": sum(g > 11 for g in gaps),
                }
            out["learning_24h"] = _one(conn, """
                SELECT COUNT(*) AS cycles, SUM(math_issued) AS issued,
                       SUM(math_settled) AS settled,
                       SUM(CASE WHEN math_ok=0 THEN 1 ELSE 0 END) AS math_failed,
                       SUM(CASE WHEN model_review_ok=0 THEN 1 ELSE 0 END) AS review_failed,
                       MAX(completed_at) AS last
                FROM perp_learning_cycle_health WHERE completed_at>=?""", (day,))
            horizons = {}
            for h in (5, 15, 60):
                horizons[h] = _one(conn, """
                    SELECT COUNT(*) AS issued, COUNT(DISTINCT target_opened_at) AS windows,
                           COUNT(DISTINCT venue) AS venues, MIN(issued_at) AS first_issued,
                           SUM(CASE WHEN settled_at IS NULL AND target_opened_at<=? THEN 1 ELSE 0 END) AS overdue,
                           SUM(CASE WHEN settled_at IS NULL THEN 1 ELSE 0 END) AS unsettled
                    FROM perp_forward_audit
                    WHERE asset='BTC' AND model_version=? AND horizon_minutes=? AND issued_at>=?""",
                    ((now - timedelta(minutes=65)).isoformat(), MODEL, h, week))
            out["btc_7d"] = horizons
            out["btc_issued_by_hour_24h"] = [
                [str(r["hr"]), int(r["n"]), int(r["w"])] for r in conn.execute("""
                    SELECT SUBSTR(issued_at,1,13) AS hr, COUNT(*) AS n,
                           COUNT(DISTINCT target_opened_at) AS w
                    FROM perp_forward_audit
                    WHERE asset='BTC' AND horizon_minutes=5 AND model_version=? AND issued_at>=?
                    GROUP BY SUBSTR(issued_at,1,13) ORDER BY hr""", (MODEL, day)).fetchall()]
            out["models_7d"] = [
                [str(r["model_version"]), int(r["n"])] for r in conn.execute(
                    "SELECT model_version, COUNT(*) AS n FROM perp_forward_audit "
                    "WHERE issued_at>=? GROUP BY model_version", (week,)).fetchall()]
            out["lineage_columns"] = [str(r["column_name"]) for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='perp_forward_audit'").fetchall()]
        monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        for name, fn in (
            ("summary", lambda: monitor.summary(asset="BTC")["last_7d"][5]),
            ("benefit", lambda: monitor.active_learning_evidence(asset="BTC", horizon_minutes=5)),
            ("lineage", lambda: monitor.learning_lineage_summary(asset="BTC", horizon_minutes=5)),
        ):
            seconds, result, error = _timed(fn)
            if isinstance(result, dict):
                result = {k: v for k, v in result.items()
                          if isinstance(v, (int, float, str, bool)) or v is None}
            out[name] = {"seconds": seconds, "error": error, "result": result}
        # The live Discord page gives each reader 4 s; run benefit twice to
        # separate a cold first query from a consistently slow one.
        out["benefit_second_run_seconds"] = _timed(
            lambda: monitor.active_learning_evidence(asset="BTC", horizon_minutes=5))[0]
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
