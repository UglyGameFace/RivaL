"""Read-only recorder-integrity sweep for the registered tests.

Rectification queue #2 (2026-11-27): the G-C outcome recorder crashed
on live DB row format while passing tests, and stayed silent until
someone checked. This sweep makes that failure shape a number instead
of a missing verdict: for every registered test readable from the
bot's store it reports rows recorded vs rows evaluable, with the
evaluator's own exclusion reasons, straight from live data.

SELECT-only -- it never writes to the store, never issues a forecast,
and is not wired into any bot loop or schedule. Each section fails
independently: a section that cannot be read reports
``read_unavailable`` with the exception type, never invented numbers.
N3's recorder runs outside this store, and no N2 (weather) grading
module or table exists in this tree; both say so plainly.

Usage (on the host, from the deployed source tree):
    python tools/recorder_integrity_sweep.py [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_RECORDER_INTEGRITY="


def _section(fn) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one section must not sink the sweep
        return {"status": "read_unavailable", "error": type(exc).__name__}


def _convergence(store) -> dict:
    from parlay_bot.perpetuals import convergence_evaluation as ce

    start = ce.COLLECTION_START
    epoch_start = start.replace(
        minute=(start.minute // 15) * 15, second=0, microsecond=0)
    epoch = (epoch_start.isoformat(), ce.ACCRUAL_DEADLINE.isoformat())
    with store.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS rows_seen,"
            " COUNT(DISTINCT asset || '|' || window_open) AS windows_recorded,"
            " COUNT(DISTINCT contract_id) AS contracts"
            " FROM rival_convergence_book_snapshots"
            " WHERE window_open>=? AND window_open<?", epoch).fetchone()
    report = ce.evaluate_windows(ce.load_windows(store),
                                 now=datetime.now(UTC))
    coverage = report["coverage"]
    return {
        "status": report["status"], "verdict": report["verdict"],
        "recorded": {"snapshot_rows": int(row["rows_seen"]),
                     "windows": int(row["windows_recorded"]),
                     "contracts": int(row["contracts"])},
        "evaluable": {"windows_seen": coverage["windows_seen"],
                      "windows_paired_both_venues":
                          coverage["windows_paired_both_venues"],
                      "windows_with_entry_and_exit_checkpoints":
                          coverage["windows_with_entry_and_exit_checkpoints"],
                      "trigger_windows": coverage["trigger_windows"],
                      "trigger_distinct_days":
                          coverage["trigger_distinct_days"]},
        "exclusions": coverage["exclusions"],
        "checkpoint_missing": coverage["checkpoint_missing"],
    }


def _pm_gap(store, fv_windows: list[dict]) -> dict:
    """Why Polymarket fair-value windows fail to evaluate, from raw data.

    Splits the evaluator's settlement_unavailable count by asset and
    cross-checks the outcome recorder's own tables: a missing contract
    that IS in rival_polymarket_observations and HAS resolution
    attempts is a recorder that keeps trying and never lands -- the
    shape that hid the SOL/XRP Chainlink-reported-resolution defect
    (empty transaction_hash rejected on the hash check alone).
    """
    with store.connect() as conn:
        missing_by_asset = {
            str(r["asset"]): int(r["missing"]) for r in conn.execute(
                "SELECT s.asset AS asset,"
                " COUNT(DISTINCT s.contract_id) AS missing"
                " FROM rival_convergence_book_snapshots s"
                " WHERE s.venue='polymarket' AND NOT EXISTS ("
                " SELECT 1 FROM rival_polymarket_ai_outcomes o"
                " WHERE o.slug=s.contract_id)"
                " GROUP BY s.asset").fetchall()}
        row = conn.execute(
            "SELECT"
            " COUNT(DISTINCT s.contract_id) AS missing,"
            " COUNT(DISTINCT CASE WHEN EXISTS ("
            "  SELECT 1 FROM rival_polymarket_observations v"
            "  WHERE v.slug=s.contract_id) THEN s.contract_id END)"
            "  AS covered_by_observations,"
            " COUNT(DISTINCT CASE WHEN EXISTS ("
            "  SELECT 1 FROM rival_polymarket_resolution_attempts a"
            "  WHERE a.slug=s.contract_id) THEN s.contract_id END)"
            "  AS attempted"
            " FROM rival_convergence_book_snapshots s"
            " WHERE s.venue='polymarket' AND NOT EXISTS ("
            " SELECT 1 FROM rival_polymarket_ai_outcomes o"
            " WHERE o.slug=s.contract_id)").fetchone()
        prefix = ("split_part(slug, '-', 1)" if store.database.backend == "postgres"
                  else "substr(slug, 1, instr(slug, '-') - 1)")
        last_outcome = {
            str(r["asset"]): r["last_resolved"] for r in conn.execute(
                f"SELECT {prefix} AS asset,"
                " MAX(resolved_at) AS last_resolved"
                " FROM rival_polymarket_ai_outcomes"
                " WHERE slug LIKE '%-updown-15m-%' GROUP BY 1").fetchall()}
    # missing_snapshot is observation-level: a window that passed the
    # window checks but has no snapshot within the checkpoint tolerance
    # of an offset. Count windows by their weakest checkpoint directly.
    from parlay_bot.perpetuals.convergence_quotes import select_checkpoint

    weak = 0
    for window in fv_windows:
        if window.get("venue") != "polymarket":
            continue
        rows = window.get("snapshots") or []
        if any(select_checkpoint(rows, window_open=window["window_open"],
                                 offset_seconds=offset) is None
               for offset in (300, 600)):
            weak += 1
    return {
        "contracts_missing_outcome": int(row["missing"]),
        "missing_by_asset": missing_by_asset,
        "missing_covered_by_observations": int(row["covered_by_observations"]),
        "missing_with_resolution_attempts": int(row["attempted"]),
        "last_outcome_resolved_by_asset_15m": last_outcome,
        "pm_windows_missing_a_checkpoint_snapshot": weak,
    }


def _fair_value(store) -> dict:
    from parlay_bot.perpetuals import fair_value_evaluation as fve

    windows = fve.load_windows(store)
    report = fve.evaluate_fair_value(windows, now=datetime.now(UTC))
    out: dict = {"venues": {}}
    for venue in fve.VENUES:
        block = report["venues"][venue]
        recorded = [w for w in windows if w.get("venue") == venue]
        out["venues"][venue] = {
            "status": block["status"], "verdict": block["verdict"],
            "recorded": {"windows": len(recorded),
                         "snapshot_rows": sum(len(w["snapshots"])
                                              for w in recorded)},
            "evaluable": {"windows_seen": block["coverage"]["windows_seen"],
                          "evaluated_windows":
                              block["coverage"]["evaluated_windows"],
                          "primary_observations":
                              block["coverage"]["primary_observations"]},
            "floors": block["floors"],
            "window_exclusions": block["coverage"]["window_exclusions"],
            "observation_exclusions":
                block["coverage"]["observation_exclusions"],
        }
    out["polymarket_gap"] = _pm_gap(store, windows)
    return out


def _g_c(store) -> dict:
    from parlay_bot.perpetuals import replay_generation as rg

    coverage = rg.estate_coverage(store)
    opt_rows, opt_cov = rg.load_gc_opt_implied(store)
    gap_rows, gap_cov = rg.load_gc_venue_gap(store)
    return {
        "status": "readable",
        "recorded": coverage,
        "evaluable": {
            "opt_implied_btc_weekly": {
                "observations": len(opt_rows), "coverage": opt_cov},
            "pm_kalshi_threshold_gap": {
                "observations": len(gap_rows), "coverage": gap_cov},
        },
    }


def _g_d(store) -> dict:
    from parlay_bot.perpetuals import replay_generation as rg

    rows, coverage = rg.load_gd_record(store)
    return {"status": "readable", "recorded_vs_evaluable": coverage,
            "evaluable_rows": len(rows)}


def _n2(store) -> dict:
    with store.connect() as conn:
        if store.database.backend == "postgres":
            found = conn.execute(
                "SELECT table_name AS name FROM information_schema.tables"
                " WHERE table_name LIKE '%weather%'").fetchall()
        else:
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name LIKE '%weather%'").fetchall()
    names = [str(r["name"]) for r in found]
    return {"status": "not_readable",
            "weather_tables_found": names,
            "note": ("No N2 (weather) grading module exists in this tree "
                     "and no weather table is present in this store, so "
                     "its recorder->evaluator path cannot be swept from "
                     "here. Reported plainly, not estimated.")}


def _n3() -> dict:
    return {"status": "external_recorder",
            "note": ("N3's recorder runs outside the bot's database (its "
                     "own prospective store), so its counters cannot be "
                     "read from this container.")}


def sweep(store) -> dict:
    return {
        "schema": "rival-recorder-integrity-v1",
        "computed_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "tests": {
            "convergence": _section(lambda: _convergence(store)),
            "fair_value": _section(lambda: _fair_value(store)),
            "g_c": _section(lambda: _g_c(store)),
            "g_d": _section(lambda: _g_d(store)),
            "n2": _section(lambda: _n2(store)),
            "n3": _n3(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only recorder-integrity sweep (registered tests)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Also write the JSON report to this path",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    if settings.rival_storage_backend == "postgres":
        if settings.rival_database_url is None:
            print(json.dumps({
                "status": "UNVERIFIED", "reason": "host database not configured",
            }))
            return 2
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    else:
        store = SQLiteHotStore(settings.rival_db_path)
    report = sweep(store)
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
