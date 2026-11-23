"""Pinned-input canary for the Slice 2 proof-review move (2026-11-23).

The rival-worker service drives the 20-minute ``FamilyPromotion``
proof review out of process. Before the bot's in-cycle driver is
flagged off, this harness proves the worker's computation is the bot's
computation: for one slot it recomputes the evaluation with the stamp
pinned to the slot's evaluation stamp -- the exact inputs both drivers
use -- inside a snapshot isolation that restores ``rival_family_promotion``,
its receipts, and the offline-pin rows byte for byte, then compares
the SHA-256 of the full promotion row the evaluation wrote against
``--expect-sha256`` (the hash the worker's shadow cycle logged for the
same slot). The stamp is the slot's evaluation stamp (boundary +
``PROMOTION_STAMP_OFFSET_SECONDS``), derived by ``promotion_driver``
itself -- this harness contains no stamp math of its own. MATCH is
byte-identity of the entire evaluation record -- status, selection,
reason, and the full evidence JSON -- not just of a promotion event
(promotion has never fired; the held record is what must match).

Only hashes, the slot, and the scope are ever printed -- never payload
contents. Exit status: 0 on MATCH (or COMPUTED when no expectation was
given), 1 on DIFF/ERROR/NO_SCOPE/RACED, 2 on PROMOTION_DETECTED (a
shadow evaluation selected a family; its state was deliberately left
in place for human review and the flip must stop).

Usage (on the host, from the deployed source tree):
    python tools/family_promotion_canary.py [--slot N]
        [--expect-sha256 HEX] [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.perpetuals.promotion_driver import run_shadow_canary
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_PROMOTION_CANARY="


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
        description="Pinned-input canary for the worker proof review",
    )
    parser.add_argument(
        "--slot", type=int, default=None,
        help="20-minute proof slot to prove (default: most recent "
             "completed slot)",
    )
    parser.add_argument(
        "--expect-sha256", type=str, default=None,
        help="Row hash the worker's shadow cycle logged for this slot",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Also write the JSON report to this path",
    )
    args = parser.parse_args(argv)

    store = build_store(Settings())
    try:
        report = run_shadow_canary(
            store, slot=args.slot, expected_sha256=args.expect_sha256)
    finally:
        store.close()
    scope = report["scope"]
    scope_label = ":".join(str(part) for part in scope) if scope else "none"
    print(f"{report['canary_status']:18s} slot={report['slot']} "
          f"scope={scope_label} status={report['status']} "
          f"row={report['row_sha256']} expected={args.expect_sha256}")
    print(MARKER + json.dumps(
        {"slot": report["slot"], "stamp": report["stamp"],
         "canary_status": report["canary_status"],
         "row_sha256": report["row_sha256"]},
        sort_keys=True))
    if args.output is not None:
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str),
            encoding="utf-8")
    if report["canary_status"] == "PROMOTION_DETECTED":
        return 2
    if report["canary_status"] in ("MATCH", "COMPUTED"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
