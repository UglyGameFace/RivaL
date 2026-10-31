"""Read the actual hosted PostgreSQL acceptance evidence without Discord.

Usage on the authorized RivaL host with RIVAL_DATABASE_URL configured:
    python tools/unattended_acceptance.py

Exit 0 requires an actual source-matched quote + independently later
settled challenger comparison. This is not model promotion approval.
"""
from __future__ import annotations

import json
import sys

from parlay_bot.config import Settings
from parlay_bot.perpetuals.unattended_acceptance import unattended_acceptance_receipt
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def main() -> int:
    settings = Settings()
    if settings.rival_storage_backend == "postgres":
        if settings.rival_database_url is None:
            print(json.dumps({"status": "UNVERIFIED", "reason": "host database not configured"}))
            return 2
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    else:
        store = SQLiteHotStore(settings.rival_db_path)
    try:
        receipt = unattended_acceptance_receipt(store)
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
