"""Run RivaL's production acceptance and health checks from a deployed source tree.

Hosted entry point: python main.py --acceptance
The implementation is packaged under src/ alongside the running bot so a
disconnected tools/ directory cannot prevent production diagnostics.
"""
from __future__ import annotations

import argparse
import json

from parlay_bot.config import Settings
from parlay_bot.perpetuals.host_acceptance import (
    capture_restart_baseline,
    host_health,
    verify_restart_baseline,
)
from parlay_bot.perpetuals.unattended_acceptance import unattended_acceptance_receipt
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RivaL host acceptance, watchdog and process-restart evidence",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--acceptance", action="store_true",
        help="Check causal live challenger and matching quote evidence (default)",
    )
    modes.add_argument(
        "--health", action="store_true",
        help="Read persisted watchdog heartbeat, process boot, and incidents",
    )
    modes.add_argument(
        "--mark-restart", action="store_true",
        help="Snapshot finalized rows before restarting RivaL on Discloud",
    )
    modes.add_argument(
        "--verify-restart", action="store_true",
        help="Verify retained rows, a new bot boot, and post-restart forecasts",
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

    try:
        if args.mark_restart:
            result = capture_restart_baseline(store)
            passed = result["status"] == "BASELINE_SAVED"
        elif args.verify_restart:
            result = verify_restart_baseline(store)
            passed = result["status"] == "PASS"
        elif args.health:
            result = host_health(store)
            # Underperforming experimental shadow models are reported but
            # cannot mark an otherwise live bot process as unavailable.
            passed = result["operational_status"] == "HEALTHY"
        else:
            result = unattended_acceptance_receipt(store)
            try:
                result["host_health"] = host_health(store)
            except Exception as exc:  # noqa: BLE001 - a live receipt cannot invent health
                result["host_health"] = {
                    "status": "UNVERIFIED", "reason": type(exc).__name__,
                }
            passed = result["status"] == "HOST_EVIDENCE_PRESENT"
        print(json.dumps(result, sort_keys=True))
        return 0 if passed else 2
    except Exception as exc:  # noqa: BLE001 - fail closed without exposing credentials
        print(json.dumps({
            "status": "UNVERIFIED",
            "reason": f"host evidence query failed: {type(exc).__name__}",
        }))
        return 2
    finally:
        store.close()
