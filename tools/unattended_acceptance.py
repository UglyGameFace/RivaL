"""Read the actual hosted PostgreSQL acceptance evidence without Discord.

Usage on the authorized RivaL host with RIVAL_DATABASE_URL configured:
    python tools/unattended_acceptance.py

Exit 0 requires an actual source-matched quote + independently later
settled challenger comparison. This is not model promotion approval.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    # Discloud runs source files in-place and doesn't pip-install this package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.host_acceptance import (
        capture_restart_baseline,
        host_health,
        verify_restart_baseline,
    )
    from parlay_bot.perpetuals.unattended_acceptance import unattended_acceptance_receipt
    from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

    parser = argparse.ArgumentParser(
        description="RivaL production evidence, health, and restart acceptance"
    )
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--mark-restart", action="store_true",
        help="Save settled immutable evidence before a controlled bot restart",
    )
    options.add_argument(
        "--verify-restart", action="store_true",
        help="Compare preserved rows, bot process identity and new forecasts",
    )
    flags = parser.parse_args()
    settings = Settings()
    if settings.rival_storage_backend == "postgres":
        if settings.rival_database_url is None:
            print(json.dumps({"status": "UNVERIFIED", "reason": "host database not configured"}))
            return 2
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    else:
        store = SQLiteHotStore(settings.rival_db_path)
    try:
        if flags.mark_restart:
            receipt = capture_restart_baseline(store)
            print(json.dumps(receipt, sort_keys=True))
            return 0 if receipt["status"] == "BASELINE_SAVED" else 2
        if flags.verify_restart:
            receipt = verify_restart_baseline(store)
            print(json.dumps(receipt, sort_keys=True))
            return 0 if receipt["status"] == "PASS" else 2
        receipt = unattended_acceptance_receipt(store)
        try:
            receipt["host_health"] = host_health(store)
        except Exception as exc:  # noqa: BLE001 - report degraded evidence, never fabricate a pass
            receipt["host_health"] = {
                "status": "UNVERIFIED", "reason": type(exc).__name__,
            }
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["status"] == "HOST_EVIDENCE_PRESENT" else 2
    except Exception as exc:  # noqa: BLE001 - missing tables aren't proof of success
        print(json.dumps({
            "status": "UNVERIFIED",
            "reason": f"host evidence query failed: {type(exc).__name__}",
        }))
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
