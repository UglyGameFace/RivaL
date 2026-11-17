"""Restore verified archive months for one asset (Phase 1, GHA step 1).

Uses the existing restore bridges; a venue that fails is listed in the
restore report, never silently skipped. Binance 5m is loaded separately
by the trainer via --binance-cache (official Vision ZIPs).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from parlay_bot.research.archive_training import restore_verified_months
from parlay_bot.research.batch import complete_months


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore verified archive months for one asset.")
    parser.add_argument("--asset", required=True, choices=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument(
        "--months", nargs="+", default=None, help="YYYY-MM months (default: last 6 completed)"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--okx-source-integrity",
        choices=("okx-v5-public-history-5m-v1",
                 "okx-v5-official-1m-zip-derived-5m-v1"),
        default=None,
        help="Explicit OKX family for source-authenticated research; default remains strict",
    )
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="Freeze source restoration at the collector UTC date")
    parser.add_argument("--records-since", type=date.fromisoformat,
                        help="First daily ledger backup date (default: last 90 days)")
    parser.add_argument("--records-through", type=date.fromisoformat,
                        help="Last completed UTC backup date (default: yesterday)")
    args = parser.parse_args(argv)
    if args.as_of and args.as_of > datetime.now(UTC).date():
        parser.error("source-run date cannot be in the future")
    clock = (datetime.combine(args.as_of, time.min, tzinfo=UTC)
             if args.as_of else datetime.now(UTC))
    months = args.months or complete_months(now=clock, count=6)
    since = (args.records_since if args.records_since is not None else
             args.as_of - timedelta(days=90) if args.as_of else None)
    through = (args.records_through if args.records_through is not None else
               args.as_of - timedelta(days=1) if args.as_of else None)
    report = asyncio.run(restore_verified_months(
        asset=args.asset, months=months, root=args.root,
        records_since=since, records_through=through, now=clock,
        okx_source_integrity=args.okx_source_integrity))
    print(f"restored: { {k: len(v) for k, v in report['restored'].items()} }")
    print(f"excluded: {report['excluded']}")
    print(f"okx_selected_source_integrity: {report['okx_selected_source_integrity']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
