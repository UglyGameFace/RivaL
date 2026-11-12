"""Prediction cleanup the owner approved on Nov 11, 2026.

Two parts, both permanent deletes, both in small batches (each on its own
short connection, so no long lock and no long-held pooled connection):

1. **Retired models, once.** Their calls are not shown anywhere and can
   never advance: liq_v1 (and its input table rival_liq1_features),
   flow_v1/v2/v3, the reverted per-exchange '<family>_venue' variants (#365,
   reverted in #366), and three replaced 6-year versions.
2. **Age rule, daily.** Predictions older than ``KEEP_DAYS`` (45 days: the
   longest read-back is the 30-day promotion check, plus margin), and
   test-model calls whose target is more than ``UNGRADABLE_AFTER`` (14 days)
   old and still ungraded: their main-model result never arrived (a target
   with no trades is withdrawn), so they never will be.

Nothing else is touched: promotion records, early stops, quotes and the
Model Lab tables stay.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

KEEP_DAYS = 45
UNGRADABLE_AFTER = timedelta(days=14)
BATCH = 5000
MAX_BATCHES = 400        # per table per run: at most 2M rows, then the next run

RETIRED_FAMILIES = (
    "liq_v1", "flow_v1", "flow_v2", "flow_v3",
    "pooled_15m_218257cd35a7", "pooled_60m_798009383b39", "pooled_5m_dea81c66b992",
)
RETIRED_SUFFIX = "_venue"

# Predictions and the per-prediction records keyed to them.
AGED_TABLES = (
    ("rival_model_shadow_forecasts", "issued_at"),
    ("perp_forward_audit", "issued_at"),
    ("rival_forward_model_lineage", "issued_at"),
    ("perp_forward_regimes", "issued_at"),
    ("rival_forward_path_targets", "issued_at"),
)


def _delete_batched(store, table: str, where: str, params: tuple) -> int:
    """Delete matching rows BATCH at a time; returns how many went."""
    postgres = getattr(getattr(store, "database", None), "backend", "") == "postgres"
    key = "ctid" if postgres else "rowid"
    total = 0
    for _ in range(MAX_BATCHES):
        with store.connect() as conn:
            result = conn.execute(
                f"DELETE FROM {table} WHERE {key} IN "
                f"(SELECT {key} FROM {table} WHERE {where} LIMIT {BATCH})", params)
            removed = int(result.rowcount or 0)
        total += removed
        if removed < BATCH:
            break
    return total


def _exists(store, table: str) -> bool:
    try:
        with store.connect() as conn:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
        return True
    except Exception:  # noqa: BLE001 - a table that is not there has nothing to clean
        return False


def run(store, now: datetime | None = None) -> dict[str, int]:
    """One cleanup pass; returns rows deleted per part. Safe to repeat."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    done: dict[str, int] = {}
    if _exists(store, "rival_model_shadow_forecasts"):
        marks = ",".join("?" for _ in RETIRED_FAMILIES)
        done["retired_model_calls"] = _delete_batched(
            store, "rival_model_shadow_forecasts",
            f"(family IN ({marks}) OR family LIKE ?)",
            (*RETIRED_FAMILIES, f"%{RETIRED_SUFFIX}"))
        done["ungradable_calls"] = _delete_batched(
            store, "rival_model_shadow_forecasts",
            "settled_at IS NULL AND target_opened_at<?",
            ((stamp - UNGRADABLE_AFTER).isoformat(),))
    if _exists(store, "rival_liq1_features"):
        done["retired_liq_inputs"] = _delete_batched(store, "rival_liq1_features", "1=1", ())
    cutoff = (stamp - timedelta(days=KEEP_DAYS)).isoformat()
    for table, column in AGED_TABLES:
        if _exists(store, table):
            done[f"older_than_{KEEP_DAYS}d:{table}"] = _delete_batched(
                store, table, f"{column}<?", (cutoff,))
    if any(done.values()):
        _LOG.info("RivaL prediction cleanup deleted %s",
                  ", ".join(f"{k}={v}" for k, v in done.items() if v))
    return done
