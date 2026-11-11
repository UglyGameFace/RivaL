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
