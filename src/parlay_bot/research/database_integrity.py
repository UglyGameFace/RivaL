"""Read-only, bounded-frequency integrity counters for RivaL's live PostgreSQL.

The counters never mutate the ledger or infer profits from synthetic quotes.
They run inside the existing Discloud process and log aggregates only; the
private database connection string and individual user records stay private.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.storage.hot import RelationalHotStore


def sports_settlement_backlog(store: RelationalHotStore) -> int:
    """Finalized scores that exist but have not resolved a forecast."""
    with store.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n
               FROM rival_sport_forecasts AS f
               JOIN rival_sport_results AS r ON r.event_id=f.event_id
               WHERE f.settled_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM rival_sport_settlement_exclusions AS e
                   WHERE e.event_id=f.event_id
                 )"""
        ).fetchone()
    return int(row["n"])


def sports_settlement_exclusions(store: RelationalHotStore) -> dict[str, int]:
    """Report terminal exclusions separately from actionable settlement backlog."""
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT reason,COUNT(*) AS n FROM rival_sport_settlement_exclusions
               GROUP BY reason"""
        ).fetchall()
    return {str(row["reason"]): int(row["n"]) for row in rows}



def sports_runtime_snapshot(
    store: RelationalHotStore, *, now: datetime | None = None,
) -> dict:
    """Read actual hosted final/result/forecast progress, never infer a win.

    The persisted last-cycle record is diagnostic, NOT settlement proof.
    Count finalized events and scored forecasts from immutable SQL ledgers.
    """
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("Runtime evidence needs a timezone-aware clock")
    with store.connect() as conn:
        finals = conn.execute(
            """SELECT COUNT(*) AS n,MAX(first_observed_at) AS latest
               FROM rival_sport_results"""
        ).fetchone()
        scored = conn.execute(
            """SELECT COUNT(*) AS n,MAX(settled_at) AS latest
               FROM rival_sport_forecasts WHERE settled_at IS NOT NULL"""
        ).fetchone()
        pending = conn.execute(
            """SELECT COUNT(*) AS n FROM fixtures AS f
               LEFT JOIN rival_sport_results AS r
                 ON r.event_id=f.fixture_id
               WHERE f.provider='sportsgameodds'
                 AND f.sport_id IN ('SOCCER','BASKETBALL')
                 AND f.start_time IS NOT NULL AND f.start_time<?
                 AND r.event_id IS NULL""",
            (stamp.astimezone(UTC).isoformat(),),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT COUNT(*) AS n FROM rival_sport_forecasts AS f
               JOIN rival_sport_results AS r ON r.event_id=f.event_id
               WHERE f.settled_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM rival_sport_settlement_exclusions AS e
                   WHERE e.event_id=f.event_id
                 )"""
        ).fetchone()
        excluded = conn.execute(
            """SELECT reason,COUNT(*) AS n FROM rival_sport_settlement_exclusions
               GROUP BY reason"""
        ).fetchall()
        cycle = conn.execute(
            """SELECT state_value FROM runtime_state
               WHERE state_key='rival:sports-results:last-cycle'"""
        ).fetchone()
    last_cycle = None
    if cycle is not None:
        try:
            loaded = json.loads(cycle["state_value"])
            if isinstance(loaded, dict) and isinstance(loaded.get("at"), str):
                last_cycle = loaded
        except (TypeError, ValueError):
            pass
    return {
        "final_events": int(finals["n"]),
        "last_final_at": finals["latest"],
        "scored_forecasts": int(scored["n"]),
        "last_scored_at": scored["latest"],
        "unsettled_finalized": int(unresolved["n"]),
        "exclusions": {str(r["reason"]): int(r["n"]) for r in excluded},
        "started_missing_results": int(pending["n"]),
        "last_cycle": last_cycle,
    }

def perpetual_evidence_backlog(
    store: RelationalHotStore, *, now: datetime | None = None,
) -> dict[str, int]:
    """Overdue forward rows, safely recoverable rows, and non-trade quote pairs."""
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("Database audit requires a timezone-aware clock")
    stamp = stamp.astimezone(UTC)
    cutoff = (stamp - timedelta(days=2)).isoformat()
    with store.connect() as conn:
        overdue = conn.execute(
            """SELECT COUNT(*) AS n
               FROM perp_forward_audit AS f
               WHERE f.settled_at IS NULL
                 AND f.target_opened_at<?""",
            (cutoff,),
        ).fetchone()
        recoverable = conn.execute(
            """SELECT COUNT(*) AS n
               FROM perp_forward_audit AS f
               JOIN perp_market_candles AS c
                 ON c.venue=f.venue AND c.contract=f.contract
                AND c.quote=f.quote AND c.interval_minutes=5
                AND c.opened_at=f.target_opened_at
               WHERE f.settled_at IS NULL
                 AND f.target_opened_at<?
                 AND c.first_observed_at>f.issued_at
                 AND c.first_observed_at<=?
                 AND c.closed_at<?""",
            (cutoff, stamp.isoformat(), stamp.isoformat()),
        ).fetchone()
        quotes = conn.execute(
            """SELECT COUNT(*) AS closed,
                      SUM(CASE WHEN e.probability_up>0.44
                                     AND e.probability_up<0.56
                                     AND e.reference_probability_up>0.44
                                     AND e.reference_probability_up<0.56
                               THEN 1 ELSE 0 END) AS neither_traded
               FROM perp_multi_quote_shadow AS q
               JOIN perp_multi_forward AS e
                 ON e.asset=q.asset AND e.horizon_minutes=q.horizon_minutes
                AND e.target_opened_at=q.target_opened_at
               WHERE q.state='EXIT_QUOTED'"""
        ).fetchone()
    return {
        "overdue_forecasts": int(overdue["n"]),
        "recoverable_overdue": int(recoverable["n"]),
        "closed_quote_windows": int(quotes["closed"]),
        "neither_traded_quote_windows": int(quotes["neither_traded"] or 0),
    }
