"""Private Discloud Python launcher for RivaL's out-of-process worker.

Slice 1: every five minutes this service runs the bot's own uncached
scorecard aggregates (imported, never copied) and upserts the results
into ``rival_materialized_reads`` so the bot's Performance surfaces can
point-read them behind a default-off flag.

Slice 2: every twenty minutes, on the bot cycle's exact proof-review
slot schedule, this service drives ``FamilyPromotion.evaluate`` for the
slot's scope -- the same selection functions, the same pinned slot
stamp, the same idempotent upsert into ``rival_family_promotion`` that
every Discord reader already point-reads. The bot keeps driving until
``RIVAL_PROMOTION_WORKER_DRIVEN`` is set on the bot, so exactly one
process writes at any time.

The deployment zip carries this folder's files at the app root together
with the repo's ``src/`` tree (the same layout the bot's root ``main.py``
expects), so the package is importable after the path bootstrap below.
In a repo checkout the same bootstrap finds ``../../src``.

Only ``RIVAL_DATABASE_URL`` is required (plus the storage backend
selector). ``RIVAL_WORKER_SHADOW`` defaults to ON: the scorecard job
then writes only ``shadow:``-prefixed kinds that no reader serves.
``RIVAL_WORKER_PROMOTION_SHADOW`` defaults to ON independently: the
promotion job then computes each slot's evaluation snapshot-isolated,
logs the promotion row's SHA-256, and restores the prior state, so the
bot remains the only writer until the pinned-input canary
(``tools/family_promotion_canary.py``) proves byte-identity and the
flip is ordered.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("rival-worker")


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


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in (
        "0", "false", "no", "off")


def shadow_mode() -> bool:
    return _env_flag("RIVAL_WORKER_SHADOW")


def promotion_shadow_mode() -> bool:
    return _env_flag("RIVAL_WORKER_PROMOTION_SHADOW")


def _supervise(name: str, loop, store, *, shadow: bool) -> None:
    """Run one job loop forever; a loop that ever exits is restarted.

    Both job loops already catch per-cycle failures themselves, so this
    only guards construction-level death (e.g. a transient store error
    while building a job's computer) from silently ending a job.
    """
    while True:
        try:
            loop(store, shadow=shadow)
        except Exception:  # noqa: BLE001 - supervised restart
            _LOG.exception("%s loop exited unexpectedly", name)
        _LOG.warning("%s loop restarting in 30s", name)
        time.sleep(30)


def main() -> None:
    _bootstrap_package_path()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.materialized_scorecards import (
        run_forever as scorecard_run_forever,
    )
    from parlay_bot.perpetuals.promotion_driver import (
        run_forever as promotion_run_forever,
    )

    settings = Settings()
    store = build_worker_store(settings)
    shadow = shadow_mode()
    promotion_shadow = promotion_shadow_mode()
    _LOG.info(
        "worker starting (shadow=%s, promotion_shadow=%s)",
        shadow, promotion_shadow)
    threads = [
        threading.Thread(
            target=_supervise, name="rival-worker-scorecards",
            args=("scorecard", scorecard_run_forever, store),
            kwargs={"shadow": shadow}, daemon=True),
        threading.Thread(
            target=_supervise, name="rival-worker-promotion",
            args=("promotion", promotion_run_forever, store),
            kwargs={"shadow": promotion_shadow}, daemon=True),
    ]
    for thread in threads:
        thread.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
