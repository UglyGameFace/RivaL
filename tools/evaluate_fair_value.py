"""Read-only settlement-faithful fair-value evaluator (pre-test 2026-11-19).

Runs the single pre-registered fair-value measurement against the bot's
hot store: for each venue separately, prices native 15-minute Up/Down
windows from the venue's own recorded settlement index (Chainlink TWAP
for Polymarket, CF Benchmarks BRTI for Kalshi) and applies the frozen
criteria in ``parlay_bot.perpetuals.fair_value_evaluation``. SELECT-only
-- it never writes to the store, never issues a forecast, and is not
wired into any bot loop or schedule. Below the section-7 floors
(>= 7 recording days, >= 2,500 evaluated windows, >= 250 triggered
calls, per venue) it reports the ACCRUAL state and refuses to print a
PASS/KILL verdict. ``--extension-used`` marks that the one
pre-registered borderline extension (7 more days, once) has already
been consumed.

Usage (on the host, from the deployed source tree):
    python tools/evaluate_fair_value.py [--output report.json]
                                        [--extension-used]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.perpetuals.fair_value_evaluation import (
    evaluate_fair_value,
    load_windows,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_FAIR_VALUE_EVALUATION="


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only fair-value evaluation (frozen criteria)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Also write the JSON report to this path",
    )
    parser.add_argument(
        "--extension-used", action="store_true",
        help="The one borderline extension has already been consumed",
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
    report = evaluate_fair_value(
        windows, now=datetime.now(UTC), extension_used=args.extension_used)
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
