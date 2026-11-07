"""Restore verified archive months for one asset (Phase 1, GHA step 1).

Uses the existing restore bridges; a venue that fails is listed in the
restore report, never silently skipped. Binance 5m is loaded separately
by the trainer via --binance-cache (official Vision ZIPs).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
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
    args = parser.parse_args(argv)
    months = args.months or complete_months(now=datetime.now(UTC), count=6)
    report = asyncio.run(restore_verified_months(asset=args.asset, months=months, root=args.root))
    print(f"restored: { {k: len(v) for k, v in report['restored'].items()} }")
    print(f"excluded: {report['excluded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
