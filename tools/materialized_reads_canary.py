"""Shadow-diff canary for Slice 1 scorecard materialization (2026-11-21).

The rival-worker service computes the Performance scorecards out of
process and, in shadow mode, writes them under ``shadow:``-prefixed
kinds where no reader can serve them. This harness proves the move is
safe before any reader flips: for every scorecard key it recomputes the
payload through the live bot path (the existing uncached aggregates,
called verbatim) and compares canonical JSON byte-for-byte against the
worker's stored shadow row.

It prints one line per key with the comparison status and SHA-256
hashes/byte lengths only -- payload contents are never printed -- plus a
final ``RIVAL_MATERIALIZED_CANARY=`` marker JSON with the counts. Exit
status is 0 only when every key MATCHes. SELECT-only apart from the
schema's own CREATE TABLE IF NOT EXISTS; it never writes scorecard rows.

Usage (on the host, from the deployed source tree):
    python tools/materialized_reads_canary.py [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.perpetuals.materialized_scorecards import run_canary_report
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_MATERIALIZED_CANARY="


def build_store(settings: Settings):
    if settings.rival_storage_backend == "sqlite":
        return SQLiteHotStore(settings.rival_db_path)
    if settings.rival_database_url is None:
        raise RuntimeError(
            "RIVAL_DATABASE_URL is required when RIVAL_STORAGE_BACKEND=postgres"
        )
    return PostgresHotStore(settings.rival_database_url.get_secret_value())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Byte-identity canary for materialized scorecard reads",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Also write the JSON report to this path",
    )
    args = parser.parse_args(argv)

    store = build_store(Settings())
    try:
        report = run_canary_report(store)
    finally:
        store.close()
    for label, entry in report["entries"].items():
        print(f"{entry['status']:7s} {label} "
              f"stored={entry['stored_sha256']} ({entry['stored_bytes']}) "
              f"live={entry['live_sha256']} ({entry['live_bytes']})")
    print(MARKER + json.dumps(
        {"checked_at": report["checked_at"], "counts": report["counts"]},
        sort_keys=True))
    if args.output is not None:
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    counts = report["counts"]
    total = sum(counts.values())
    return 0 if counts.get("MATCH", 0) == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
