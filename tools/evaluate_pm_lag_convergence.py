"""Read-only PM-lag convergence evaluator (price-first pivot PR2).

Runs the single pre-registered Track 1 measurement against the bot's hot
store: pairs Polymarket/Kalshi book snapshots per 15-minute window and
applies the frozen criteria in
``parlay_bot.perpetuals.convergence_evaluation``. SELECT-only -- it never
writes to the store, never issues a forecast, and is not wired into any
bot loop or schedule. Below the frozen evaluation point (n >= 300
trigger windows over >= 7 distinct UTC days) it reports the ACCRUAL
state and refuses to print a PASS/KILL verdict.

Usage (on the host, from the deployed source tree):
    python tools/evaluate_pm_lag_convergence.py [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.perpetuals.convergence_evaluation import (
    evaluate_windows,
    load_windows,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_CONVERGENCE_EVALUATION="


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only PM-lag convergence evaluation (frozen criteria)",
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
    windows = load_windows(store)
    report = evaluate_windows(windows, now=datetime.now(UTC))
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
