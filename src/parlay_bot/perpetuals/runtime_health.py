"""Host-process health supervision for unattended read-only RivaL research.

Evidence checks use the real persisted source ledgers, never a GitHub CI
receipt or a Discord interaction. Alerts are transition-based to avoid spam.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)


def _age(now: datetime, value: object) -> timedelta | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.utcoffset() is None or parsed > now:
            return None
        return now - parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


class RuntimeHealthMonitor:
    """Independent watchdog. Never alters model scores or approval flags."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store
        self.started_at = datetime.now(UTC)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rival_runtime_health_incidents (
                    issue_key TEXT PRIMARY KEY,
                    detail TEXT NOT NULL,
                    state TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    occurrence_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rival_runtime_health_checkpoints (
                    component TEXT PRIMARY KEY,
                    checked_at TEXT NOT NULL
                );
            """)
        self._initialized = True

    def inspect(
        self, *, now: datetime | None = None,
        expected_feeds: tuple[tuple[str, str], ...] = (),
        learning_enabled: bool = False,
        streaming_enabled: bool = False,
        import_enabled: bool = False,
        import_checked_at: datetime | None = None,
        import_status: str = "",
    ) -> dict[str, str]:
        """Inspect causal host evidence without network calls or writes.

        Missing optional capabilities aren't treated as failures. A newly
        started process has a bounded warm-up window for its first cycle.
        """
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("health check requires a UTC-aware clock")
        stamp = stamp.astimezone(UTC)
        warm = stamp - self.started_at >= timedelta(minutes=18)
        faults: dict[str, str] = {}
        self.initialize()
        with self.store.connect() as conn:
            active = {
                row["issue_key"] for row in conn.execute(
                    "SELECT issue_key FROM rival_runtime_health_incidents WHERE state='OPEN'"
                ).fetchall()
            }
            if learning_enabled:
                cycle = conn.execute(
                    """SELECT completed_at,math_ok,ai_settlement_ok,model_review_ok
                       FROM perp_learning_cycle_health
                       ORDER BY completed_at DESC LIMIT 1"""
                ).fetchone()
                age = _age(stamp, cycle["completed_at"]) if cycle else None
                if age is None and (warm or "learning_missing" in active):
                    faults["learning_missing"] = "no valid completed learning cycle"
                elif age is not None and age > timedelta(minutes=16):
                    faults["learning_stalled"] = f"last completed cycle {int(age.total_seconds()//60)}m ago"
                if cycle and not all(int(cycle[k]) for k in (
                    "math_ok", "ai_settlement_ok", "model_review_ok",
                )):
                    faults["learning_failure"] = "persisted learning stage failed"
                diagnostic = conn.execute(
                    """SELECT checked_at,selected_streams,issued_forecasts,
                              insufficient_causal_labels,insufficient_training,
                              stale_last_candle,short_history
                       FROM perp_forward_cycle_diagnostics
                       ORDER BY checked_at DESC LIMIT 1"""
                ).fetchone()
                if (diagnostic and
                        int(diagnostic["selected_streams"]) > 0
                        and int(diagnostic["issued_forecasts"]) == 0
                        and int(diagnostic["insufficient_causal_labels"]) == 0
                        and int(diagnostic["insufficient_training"]) == 0
                        and int(diagnostic["stale_last_candle"]) == 0
                        and int(diagnostic["short_history"]) == 0
                        and (_age(stamp, diagnostic["checked_at"]) or timedelta.max)
                        < timedelta(minutes=16)
                ):
                    faults["missing_forecasts"] = "eligible sources selected but no forecasts issued"
            if expected_feeds:
                rows = conn.execute(
                    """SELECT venue,asset,status,last_attempt_at,last_success_at,
                              last_candle_close,failure_count
                       FROM perp_feed_health"""
                ).fetchall()
                feeds = {(r["venue"], r["asset"]): r for r in rows}
                for venue, asset in expected_feeds:
                    row = feeds.get((venue, asset))
                    key = f"feed:{venue}:{asset}"
                    if row is None:
                        if warm or key in active:
                            faults[key] = "never attempted"
                        continue
                    success_age = _age(stamp, row["last_success_at"])
                    candle_age = _age(stamp, row["last_candle_close"])
                    attempted_age = _age(stamp, row["last_attempt_at"])
                    if ((success_age is None or success_age > timedelta(minutes=20)
                         or candle_age is None or candle_age > timedelta(minutes=25)
                         or attempted_age is None or attempted_age > timedelta(minutes=20))
                            and (warm or row["last_success_at"])):
                        faults[key] = (
                            f"source={row['status']} failures={row['failure_count']} "
                            "no verified fresh completed candle"
                        )
            if learning_enabled:
                # Count only genuinely later, source-exact, settleable outcomes.
                backlog = conn.execute(
                    """SELECT COUNT(*) AS n FROM perp_forward_audit AS f
                       JOIN perp_market_candles AS c
                        ON c.venue=f.venue AND c.contract=f.contract
                       AND c.quote=f.quote AND c.interval_minutes=5
                       AND c.opened_at=f.target_opened_at
                       WHERE f.settled_at IS NULL
                       AND f.target_opened_at<?
                       AND c.first_observed_at>f.issued_at
                       AND c.first_observed_at<=? AND c.closed_at<?""",
                    ((stamp-timedelta(minutes=15)).isoformat(),
                     stamp.isoformat(), stamp.isoformat()),
                ).fetchone()
                if int(backlog["n"]) > 0:
                    faults["settlement_backlog"] = f"{backlog['n']} recoverable prospective outcomes"
            if streaming_enabled and learning_enabled:
                # No claim of an executable fill: only verified matching
                # bid/ask observations. Require a meaningful sample size.
                quote_rows = conn.execute(
                    """SELECT COUNT(*) AS n,
                              SUM(CASE WHEN q.entry_at IS NOT NULL THEN 1
                                       ELSE 0 END) AS matched
                       FROM rival_model_shadow_forecasts AS f
                       LEFT JOIN rival_family_quote_shadow AS q
                         ON q.venue=f.venue AND q.contract=f.contract
                        AND q.quote=f.quote AND q.horizon_minutes=f.horizon_minutes
                        AND q.family=f.family
                        AND q.reference_opened_at=f.reference_opened_at
                       WHERE f.venue IN ('hyperliquid','binance')
                         AND (f.probability_up>=0.56 OR f.probability_up<=0.44
                              OR f.incumbent_up>=0.56 OR f.incumbent_up<=0.44)
                         AND f.issued_at>=? AND f.issued_at<?""",
                    ((stamp-timedelta(hours=2)).isoformat(),
                     (stamp-timedelta(minutes=5)).isoformat()),
                ).fetchone()
                n = int(quote_rows["n"])
                matched = int(quote_rows["matched"] or 0)
                if n >= 12 and matched / n < 0.8:
                    faults["quote_coverage"] = (
                        f"{matched}/{n} prospective actionable family forecasts "
                        "have a source-matched book"
                    )
            if learning_enabled:
                regressions = conn.execute(
                    """SELECT venue,contract,quote,family,COUNT(*) AS n,
                              AVG(model_brier) AS model_score,
                              AVG(incumbent_brier) AS incumbent_score
                       FROM rival_model_shadow_forecasts
                       WHERE settled_at IS NOT NULL AND issued_at>=?
                         AND model_brier IS NOT NULL AND incumbent_brier IS NOT NULL
                       GROUP BY venue,contract,quote,family
                       HAVING COUNT(*)>=50""",
                    ((stamp-timedelta(days=7)).isoformat(),),
                ).fetchall()
                for row in regressions:
                    if float(row["model_score"]) > float(row["incumbent_score"]) + 0.025:
                        key = f"regression:{row['venue']}:{row['contract']}:{row['family']}"
                        faults[key] = (
                            f"paired Brier {float(row['model_score']):.4f} vs "
                            f"incumbent {float(row['incumbent_score']):.4f}, "
                            f"n={row['n']}; promotion must stay gated"
                        )
            if import_enabled:
                import_age = _age(stamp, import_checked_at)
                if import_age is None and (warm or "import_unverified" in active):
                    faults["import_unverified"] = "no verified candidate scan"
                elif import_age is not None and import_age > timedelta(hours=7):
                    faults["import_stalled"] = "candidate scan overdue by more than 7 hours"
                if import_status in ("drive_read_unverified", "candidate_registry_unavailable"):
                    faults["import_failed"] = import_status
        return faults

    def publish(
        self, faults: dict[str, str], *, now: datetime | None = None,
    ) -> list[tuple[str, str, str]]:
        """Persist incident transitions; return only new/cleared events for alerting."""
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("health transition requires aware clock")
        clock = stamp.astimezone(UTC).isoformat()
        events = []
        self.initialize()
        with self.store.connect() as conn:
            previous = {
                row["issue_key"]: row for row in conn.execute(
                    "SELECT issue_key,state,occurrence_count FROM rival_runtime_health_incidents"
                ).fetchall()
            }
            for key, detail in sorted(faults.items()):
                if key not in previous or previous[key]["state"] != "OPEN":
                    events.append(("OPEN", key, detail))
                conn.execute(
                    """INSERT INTO rival_runtime_health_incidents
                       (issue_key,detail,state,first_seen_at,last_seen_at,
                        resolved_at,occurrence_count)
                       VALUES (?,?,'OPEN',?,?,NULL,1)
                       ON CONFLICT(issue_key) DO UPDATE SET
                         detail=excluded.detail,state='OPEN',
                         first_seen_at=CASE WHEN rival_runtime_health_incidents.state='OPEN'
                            THEN rival_runtime_health_incidents.first_seen_at
                            ELSE excluded.first_seen_at END,
                         last_seen_at=excluded.last_seen_at,resolved_at=NULL,
                         occurrence_count=CASE WHEN rival_runtime_health_incidents.state='OPEN'
                            THEN rival_runtime_health_incidents.occurrence_count
                            ELSE rival_runtime_health_incidents.occurrence_count+1 END""",
                    (key, detail, clock, clock),
                )
            for key, old in previous.items():
                if old["state"] == "OPEN" and key not in faults:
                    conn.execute(
                        """UPDATE rival_runtime_health_incidents
                           SET state='RESOLVED',resolved_at=?,last_seen_at=?
                           WHERE issue_key=? AND state='OPEN'""",
                        (clock, clock, key),
                    )
                    events.append(("RESOLVED", key, "verified clear on current check"))
            conn.execute(
                """INSERT INTO rival_runtime_health_checkpoints(component,checked_at)
                   VALUES ('host_watchdog',?)
                   ON CONFLICT(component) DO UPDATE SET checked_at=excluded.checked_at""",
                (clock,),
            )
        for status, key, detail in events:
            if status == "OPEN":
                _LOG.error("RivaL HEALTH ALERT %s: %s", key, detail)
            else:
                _LOG.info("RivaL HEALTH RECOVERED %s", key)
        return events

    def snapshot(self) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT issue_key,detail,first_seen_at FROM rival_runtime_health_incidents
                   WHERE state='OPEN' ORDER BY first_seen_at LIMIT 12"""
            ).fetchall()
            pulse = conn.execute(
                """SELECT checked_at FROM rival_runtime_health_checkpoints
                   WHERE component='host_watchdog'"""
            ).fetchone()
        return {"checked_at": pulse["checked_at"] if pulse else None,
                "issues": [dict(r) for r in rows]}
