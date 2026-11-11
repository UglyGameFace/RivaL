"""Read-only production probe: are the trial table, best-per-timeframe and
Model Lab numbers on Bot Performance true?

For each coin and timeframe it prints what the screen's reader returns next
to an independent recomputation from the shadow ledger, plus the Model
Lab's own state and the grading backlog. One JSON line, aggregate values
only: no SQL, DSN, secrets or row contents beyond model names.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import time
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_TRIAL_PROBE="


def _independent(conn, *, asset, horizon, family, since):
    """7-day decided calls and how many came true, written from scratch."""
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN probability_up>0.5 AND outcome_up=1 THEN 1
                           WHEN probability_up<0.5 AND outcome_up=0 THEN 1 ELSE 0 END) AS hits,
                  AVG(incumbent_brier-model_brier) AS raw_gain,
                  COUNT(DISTINCT venue) AS venues
           FROM rival_model_shadow_forecasts
           WHERE asset=? AND horizon_minutes=? AND family=? AND issued_at>=?
             AND settled_at IS NOT NULL AND outcome_up IN (0,1)
             AND probability_up<>0.5 AND probability_up BETWEEN 0 AND 1""",
        (asset, horizon, family, since)).fetchone()
    return {"calls": int(row["n"] or 0), "right": int(row["hits"] or 0),
            "raw_gain_7d": (round(float(row["raw_gain"]), 5)
                            if row["raw_gain"] is not None else None),
            "venues": int(row["venues"] or 0)}


def _storage(store, now) -> dict:
    """Read-only: size and age of the prediction tables, to plan a cleanup."""
    out = {}
    with store.connect() as conn:
        out["tables"] = [dict(r) for r in conn.execute(
            """SELECT relname AS table, n_live_tup AS rows,
                      pg_total_relation_size(relid) AS bytes
               FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC
               LIMIT 25""").fetchall()]
        out["database_bytes"] = conn.execute(
            "SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
        cuts = {f"older_{d}d": (now - timedelta(days=d)).isoformat() for d in (14, 30, 45, 60)}
        for table, column in (("rival_model_shadow_forecasts", "issued_at"),
                              ("perp_forward_audit", "issued_at"),
                              ("rival_forward_model_lineage", "issued_at")):
            row = {"oldest": conn.execute(
                f"SELECT MIN({column}) AS m FROM {table}").fetchone()["m"]}
            for name, cut in cuts.items():
                row[name] = int(conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE {column}<?", (cut,)
                ).fetchone()["n"])
            out[table] = row
        out["shadow_never_settled_older_14d"] = int(conn.execute(
            """SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts
               WHERE settled_at IS NULL AND target_opened_at<?""",
            (cuts["older_14d"],)).fetchone()["n"])
        out["shadow_retired_families_older_14d"] = [dict(r) for r in conn.execute(
            """SELECT family, COUNT(*) AS n FROM rival_model_shadow_forecasts
               WHERE issued_at<? AND (family LIKE 'flow_%' OR family LIKE 'liq_%'
                     OR family LIKE '%_venue' OR family='regime_v2')
               GROUP BY family ORDER BY n DESC""", (cuts["older_14d"],)).fetchall()]
    return out


def _timings(store, now) -> dict:
    """Seconds for the background reads that were slow (read-only)."""
    from parlay_bot.discord_app import research_alerts, trial_board
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store), read_only=True)
    out = {}

    def timed(name, fn):
        started = time.monotonic()
        try:
            fn()
            out[name] = round(time.monotonic() - started, 2)
        except Exception as exc:  # noqa: BLE001
            out[name] = f"{type(exc).__name__} after {time.monotonic() - started:.1f}s"

    def settle_select(since):
        window = "" if since is None else " AND s.target_opened_at>=?"
        stamp = now.isoformat()
        due = (now - timedelta(minutes=5)).isoformat()
        match = """FROM perp_forward_audit f WHERE f.venue=s.venue AND f.contract=s.contract
                   AND f.quote=s.quote AND f.horizon_minutes=s.horizon_minutes
                   AND f.model_version='prospective-logistic-persistent-v3'
                   AND f.reference_opened_at=s.reference_opened_at AND f.asset=s.asset
                   AND f.issued_at=s.issued_at AND f.target_opened_at=s.target_opened_at
                   AND f.settled_at IS NOT NULL AND f.settled_at<=? AND f.outcome_up IN (0,1)"""
        params = (stamp, due) + (() if since is None else (since.isoformat(),))
        with store.connect() as conn:
            conn.execute(f"""SELECT COUNT(*) FROM (SELECT (SELECT f.settled_at {match}) AS v
                              FROM rival_model_shadow_forecasts s
                              WHERE s.settled_at IS NULL AND s.target_opened_at<?{window}) x
                             WHERE x.v IS NOT NULL""", params).fetchone()

    timed("settle_fast_6h", lambda: settle_select(now - timedelta(hours=6)))
    timed("settle_14d", lambda: settle_select(now - timedelta(days=14)))
    timed("settle_full_unbounded", lambda: settle_select(None))

    def decay():
        with store.connect() as conn:
            research_alerts._decay_rows(conn, now)

    timed("research_decay", decay)
    timed("summary_7d", lambda: monitor._summary_uncached(asset="BTC", now=now))
    timed("learning_evidence_5m", lambda: monitor.active_learning_evidence(
        asset="BTC", horizon_minutes=5, now=now))
    timed("trial_hit_rates_5m", lambda: trial_board.hit_rates(
        store, asset="BTC", horizon=5, now=now, venues=["hyperliquid"]))
    with store.connect() as conn:
        out["unsettled_shadow_rows"] = dict(conn.execute(
            """SELECT COUNT(*) AS n, MIN(target_opened_at) AS oldest
               FROM rival_model_shadow_forecasts WHERE settled_at IS NULL""").fetchone())
    return out


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.discord_app import trial_board
    from parlay_bot.perpetuals import model_lab
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    store = PostgresHotStore(settings.rival_database_url.get_secret_value(), read_only=True)
    now = datetime.now(UTC)
    week = (now - timedelta(days=trial_board.HIT_DAYS)).isoformat()
    assets = tuple(a for a in os.environ.get("RIVAL_PROBE_ASSETS", "BTC,ETH,SOL,XRP")
                   .split(",") if a)
    out: dict = {"now": now.isoformat(), "boards": {}}
    try:
        lab = model_lab.ModelLab(store)
        lab._ready = True                 # read-only: never run DDL here
        lab.learners._ready = True
        try:
            started = time.monotonic()
            rows = lab._rows()
            lab._remember(rows)
            out["lab_rows_seconds"] = round(time.monotonic() - started, 2)
            out["lab"] = [{k: r.get(k) for k in (
                "family", "horizon_minutes", "status", "opened_at", "closed_at", "reason",
                "results", "days", "gain", "fitness", "reviewed_at", "parent")}
                for r in rows]
            out["lab_descriptions"] = {r["family"]: model_lab.describe(
                model_lab._plain(r["genome"])) for r in rows}
            out["lab_summary"] = {h: {"tried": v["tried"], "active": len(v["active"]),
                                      "passed": len(v["passed"]), "closed": v["closed"],
                                      "best": v["best"], "learned": v["learned"]}
                                  for h, v in lab.summary().items()}
        except Exception as exc:  # noqa: BLE001
            out["lab_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
            try:
                out["lab_ledger"] = [dict(r) for r in conn.execute(
                    """SELECT family, horizon_minutes, COUNT(*) AS issued,
                              SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled,
                              COUNT(DISTINCT venue) AS venues, MIN(issued_at) AS first,
                              MAX(issued_at) AS last
                       FROM rival_model_shadow_forecasts
                       WHERE family LIKE 'lab_%' AND issued_at>=?
                       GROUP BY family, horizon_minutes""",
                    ((now - timedelta(days=3)).isoformat(),)).fetchall()]
                out["lab_learners"] = dict(conn.execute(
                    "SELECT COUNT(*) AS n, MAX(trained_at) AS last FROM rival_lab_learners"
                ).fetchone())
            except Exception as exc:  # noqa: BLE001
                out["lab_ledger_error"] = type(exc).__name__
            # Grading backlog: rows whose result should exist by now.
            try:
                out["grading_backlog"] = [dict(r) for r in conn.execute(
                    """SELECT horizon_minutes,
                              CASE WHEN family LIKE 'lab_%' THEN 'lab' ELSE 'other' END AS kind,
                              COUNT(*) AS overdue, MIN(target_opened_at) AS oldest
                       FROM rival_model_shadow_forecasts
                       WHERE settled_at IS NULL AND issued_at>=? AND target_opened_at<?
                       GROUP BY horizon_minutes,
                                CASE WHEN family LIKE 'lab_%' THEN 'lab' ELSE 'other' END""",
                    ((now - timedelta(days=2)).isoformat(),
                     (now - timedelta(minutes=30)).isoformat())).fetchall()]
                out["issued_last_hour"] = [dict(r) for r in conn.execute(
                    """SELECT CASE WHEN family LIKE 'lab_%' THEN 'lab' ELSE 'other' END AS kind,
                              COUNT(*) AS n
                       FROM rival_model_shadow_forecasts WHERE issued_at>=?
                       GROUP BY CASE WHEN family LIKE 'lab_%' THEN 'lab' ELSE 'other' END""",
                    ((now - timedelta(hours=1)).isoformat(),)).fetchall()]
            except Exception as exc:  # noqa: BLE001
                out["grading_error"] = type(exc).__name__
        # Slow-database warnings per hour (last 24 h), to see what changed when.
        try:
            with store.connect() as conn:
                out["problem_hours"] = [dict(r) for r in conn.execute(
                    """SELECT hour, logger, level, SUBSTR(template,1,90) AS template,
                              SUM(count) AS n
                       FROM rival_problem_log WHERE hour>=?
                       GROUP BY hour, logger, level, SUBSTR(template,1,90)
                       ORDER BY hour""",
                    ((now - timedelta(hours=26)).strftime("%Y-%m-%dT%H:00:00+00:00"),)
                ).fetchall()]
        except Exception as exc:  # noqa: BLE001
            out["problem_hours_error"] = type(exc).__name__
        if os.environ.get("RIVAL_PROBE_STORAGE"):
            out["storage"] = _storage(store, now)
            return out
        if os.environ.get("RIVAL_PROBE_PROBLEMS_ONLY"):
            out["timings"] = _timings(store, now)
            return out
        retired = trial_board.retired_families()
        for asset in assets:
            for horizon in (5, 15, 60):
                key = f"{asset}/{horizon}"
                try:
                    started = time.monotonic()
                    board = trial_board.read(store, asset=asset, horizon=horizon, now=now,
                                             retired=retired)
                    seconds = round(time.monotonic() - started, 2)
                    rows = []
                    with store.connect() as conn:
                        for r in board["rows"]:
                            rows.append({
                                "name": r.name, "family": r.family, "status": r.status,
                                "days": r.days, "results": r.results,
                                "edge_points": (round(r.gain * 1000, 2)
                                                if r.gain is not None else None),
                                "ahead": r.ahead, "exchanges": r.exchanges,
                                "cut": r.stopped, "passed": r.passed,
                                "after_fees_bps": r.after_fees_bps,
                                "shown_calls": r.calls, "shown_right": r.right,
                                "independent": _independent(
                                    conn, asset=asset, horizon=horizon, family=r.family,
                                    since=week) if r.family else None,
                            })
                    top = trial_board.best(board["rows"])
                    out["boards"][key] = {
                        "seconds": seconds, "rows": rows,
                        "trials": board.get("trials"), "passed": board.get("passed"),
                        "stops": board.get("stops"),
                        "best": top.name if top else None,
                        "leader_line": trial_board.leader_line(board["rows"]),
                        "table": trial_board.table(board["rows"]),
                    }
                except Exception as exc:  # noqa: BLE001
                    out["boards"][key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        store.close()
    return out


def main() -> int:
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001 - never print DSN or SQL
        report = {"error": type(exc).__name__}
    blob = json.dumps(report, sort_keys=True, default=str).encode()
    print(MARKER + base64.b64encode(gzip.compress(blob)).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
