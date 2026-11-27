"""Read-only late-window settlement-mechanics evaluator.

Runs the pre-registered late-window test (registered 2026-11-27,
``rival-late-window-mechanics-preregistration-20261127.md``) against the
bot's hot store: Polymarket 15-minute Up/Down windows only, the parent
fair-value model at t=840s only, windows decided at or after the
registration epoch 2026-11-27T22:45:00Z only. SELECT-only -- it never
writes to the store, never issues a forecast, and is not wired into any
bot loop or schedule. Below the frozen floors (>= 150 calls, >= 21
distinct days, >= 50 calls per half) it reports ACCRUAL and refuses to
print a PASS/KILL verdict; past the 2027-01-31 deadline with floors
unmet it reports ACCRUAL_FAILED.

Usage (on the host, from the deployed source tree):
    python tools/evaluate_late_window.py [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.perpetuals.fair_value_evaluation import load_windows
from parlay_bot.perpetuals.late_window_evaluation import evaluate_late_window
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_LATE_WINDOW_EVALUATION="


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only late-window evaluation (frozen criteria)",
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
    report = evaluate_late_window(windows, now=datetime.now(UTC))
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
