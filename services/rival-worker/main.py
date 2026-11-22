"""Private Discloud Python launcher for RivaL's scorecard worker.

Slice 1 of the out-of-process scope: every five minutes this service runs
the bot's own uncached scorecard aggregates (imported, never copied) and
upserts the results into ``rival_materialized_reads`` so the bot's
Performance surfaces can point-read them behind a default-off flag.

The deployment zip carries this folder's files at the app root together
with the repo's ``src/`` tree (the same layout the bot's root ``main.py``
expects), so the package is importable after the path bootstrap below.
In a repo checkout the same bootstrap finds ``../../src``.

Only ``RIVAL_DATABASE_URL`` is required (plus the storage backend
selector). ``RIVAL_WORKER_SHADOW`` defaults to ON: the worker then writes
only ``shadow:``-prefixed kinds that no reader serves, which is the
canary posture. Set it to ``0`` only after the 24-hour shadow canary has
proven byte-identity and the bot's reader flag is flipped.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _bootstrap_package_path() -> None:
    here = Path(__file__).resolve().parent
    for candidate in (here / "src", here.parent.parent / "src"):
        if (candidate / "parlay_bot").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return
    raise RuntimeError("parlay_bot package not found beside the worker")


def build_worker_store(settings):
    """The worker's own store and pool (max_size 4), never the bot's."""
    from parlay_bot.storage.database import PostgresDatabase
    from parlay_bot.storage.hot import RelationalHotStore, SQLiteHotStore

    if settings.rival_storage_backend == "sqlite":
        return SQLiteHotStore(settings.rival_db_path)
    if settings.rival_database_url is None:
        raise RuntimeError(
            "RIVAL_DATABASE_URL is required when RIVAL_STORAGE_BACKEND=postgres"
        )
    database = PostgresDatabase(
        settings.rival_database_url.get_secret_value(), max_size=4)
    return RelationalHotStore(database)


def shadow_mode() -> bool:
    return os.environ.get("RIVAL_WORKER_SHADOW", "1").strip().lower() not in (
        "0", "false", "no", "off")


def main() -> None:
    _bootstrap_package_path()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.materialized_scorecards import run_forever

    settings = Settings()
    store = build_worker_store(settings)
    shadow = shadow_mode()
    logging.getLogger("rival-worker").info(
        "scorecard worker starting (shadow=%s)", shadow)
    run_forever(store, shadow=shadow)


if __name__ == "__main__":
    main()
