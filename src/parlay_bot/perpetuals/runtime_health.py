"""Host-process health supervision for unattended read-only RivaL research.

Evidence checks use the real persisted source ledgers, never a GitHub CI
receipt or a Discord interaction. Alerts are transition-based to avoid spam.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from parlay_bot.observability import health_alert_scope, report_model_quality
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
# How long a challenger-regression verdict (a 7-day paired score) is reused.
REGRESSION_RECHECK = timedelta(minutes=10)
# How long the watchdog trusts a regression verdict it did not compute itself
# (three missed rechecks). Past this the verdict is unknown, not clear.
REGRESSION_STALE = timedelta(minutes=30)
# A challenger counts as in use if it issued a forecast this recently.
BACKLOG_GRACE = timedelta(minutes=10)
REGRESSION_IN_USE_WITHIN = timedelta(minutes=30)


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
        self.boot_id = uuid4().hex
        self._boot_written = False
        self._initialized = False
        # (computed_at, faults) from the last completed 7-day aggregate.
        self._regression_cache: tuple[datetime, dict[str, str]] | None = None
        # The same aggregate per stream and horizon, for the early-stop rule.
        self._paired_scores: tuple[dict, ...] = ()
        self._paired_in_use_since: str | None = None

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
                CREATE TABLE IF NOT EXISTS rival_runtime_health_boots (
                    boot_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL
                );
            """)
        self._initialized = True

    def record_boot(self) -> None:
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_runtime_health_boots(boot_id,started_at) VALUES (?,?)",
                (self.boot_id, self.started_at.isoformat()),
            )
        self._boot_written = True

    def inspect(
        self, *, now: datetime | None = None,
        expected_feeds: tuple[tuple[str, str], ...] = (),
        learning_enabled: bool = False,
        streaming_enabled: bool = False,
        import_enabled: bool = False,
        import_checked_at: datetime | None = None,
        import_status: str = "",
        regressions_inline: bool = True,
    ) -> dict[str, str]:
        """Inspect causal host evidence without network calls or writes.

        Missing optional capabilities aren't treated as failures. A newly
        started process has a bounded warm-up window for its first cycle.

        ``regressions_inline=False`` is the watchdog's mode: this pass never
        runs the 7-day challenger aggregate, it only reads the verdict that
        ``refresh_regressions`` last stored (RIVAL-P/N/M: that one SELECT took
        11-12 s and cost the whole pass its 12 s statement bound or 15 s wait,
        so nothing was published).
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
                row["issue_key"]: row["detail"] for row in conn.execute(
                    "SELECT issue_key,detail FROM rival_runtime_health_incidents "
                    "WHERE state='OPEN'"
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
                failed = [name for key, name in (
                    ("math_ok", "prediction"), ("ai_settlement_ok", "AI grading"),
                    ("model_review_ok", "hourly model review"),
                ) if cycle and not int(cycle[key])]
                if failed:
                    # Say which stage: "a stage failed" sent every reader to
                    # the logs to find out.
                    faults["learning_failure"] = (
                        "persisted learning stage failed: " + ", ".join(failed))
                diagnostic = conn.execute(
                    """SELECT checked_at,selected_streams,issued_forecasts,
                              insufficient_causal_labels,insufficient_training,
                              stale_last_candle,short_history,
                              duplicate_forecasts,target_expired
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
                        # Already issued (the first cycle after a restart
                        # re-runs the bar the old process predicted, RIVAL-Z):
                        # a reason, not a gap.
                        and int(diagnostic["duplicate_forecasts"] or 0) == 0
                        and (_age(stamp, diagnostic["checked_at"]) or timedelta.max)
                        < timedelta(minutes=16)
                ):
                    if int(diagnostic["target_expired"] or 0):
                        faults["forecasts_late"] = (
                            f"every prediction this cycle ran too late "
                            f"({int(diagnostic['target_expired'])} skipped)")
                    else:
                        faults["missing_forecasts"] = (
                            "eligible sources selected but no forecasts issued")
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
                # Count only outcomes settlement will actually accept: the same
                # rules as ForwardAccuracyMonitor._settle (candle first seen
                # after issuance, issued before the target closed, a valid
                # positive close). Rows it rejects forever were counted as
                # "recoverable" and kept the alert open; they are now
                # reported separately instead of hidden.
                # The audit/candle join is one of the slow SELECTs on the
                # 2-minute watchdog pass, and the count barely moves in
                # minutes, so reuse it for the same 10-minute window as the
                # regression cache. A new backlog is then seen up to 10
                # minutes late, and the watchdog repair below re-runs on the
                # cached verdict until the next recheck — acceptable: the
                # 5-minute cycle settles anyway and the repair is idempotent.
                cached_backlog = getattr(self, "_backlog_cache", None)
                if cached_backlog is not None and (
                        timedelta(0) <= stamp - cached_backlog[0] < REGRESSION_RECHECK):
                    backlog_n, backlog_unsettleable = (
                        cached_backlog[1], cached_backlog[2])
                else:
                    backlog = conn.execute(
                        """SELECT
                             SUM(CASE WHEN f.issued_at<c.closed_at AND c.close_price>0
                                      THEN 1 ELSE 0 END) AS n,
                             SUM(CASE WHEN f.issued_at<c.closed_at AND c.close_price>0
                                      THEN 0 ELSE 1 END) AS unsettleable
                           FROM perp_forward_audit AS f
                           JOIN perp_market_candles AS c
                            ON c.venue=f.venue AND c.contract=f.contract
                           AND c.quote=f.quote AND c.interval_minutes=5
                           AND c.opened_at=f.target_opened_at
                           WHERE f.settled_at IS NULL
                           AND f.target_opened_at<?
                           AND c.first_observed_at>f.issued_at
                           AND c.first_observed_at<=? AND c.closed_at<?""",
                        # Settlement runs once per 5-minute cycle, so an outcome
                        # candle that arrived late (after a restart or a slow
                        # venue) is legitimately unsettled until the next cycle.
                        # Only one still unsettled after two full cycles is a
                        # backlog; production showed 38 incidents that each
                        # cleared within one cycle (Nov 5).
                        ((stamp-timedelta(minutes=15)).isoformat(),
                         (stamp-BACKLOG_GRACE).isoformat(), stamp.isoformat()),
                    ).fetchone()
                    backlog_n = int(backlog["n"] or 0)
                    backlog_unsettleable = int(backlog["unsettleable"] or 0)
                    self._backlog_cache = (stamp, backlog_n, backlog_unsettleable)
                if backlog_n > 0:
                    faults["settlement_backlog"] = f"{backlog_n} recoverable prospective outcomes"
                if backlog_unsettleable > 0:
                    _LOG.info("RivaL %s unsettled forecasts can never settle "
                              "(issued after target close or invalid price)",
                              backlog_unsettleable)
            if streaming_enabled and learning_enabled:
                # No claim of an executable fill: only verified matching
                # bid/ask observations. Require a meaningful sample size.
                # The shadow/quote join is one of the slow SELECTs on the
                # 2-minute watchdog pass, and the coverage ratio barely moves
                # in minutes, so reuse it for the same 10-minute window as
                # the regression cache.
                cached_quotes = getattr(self, "_quote_coverage_cache", None)
                if cached_quotes is not None and (
                        timedelta(0) <= stamp - cached_quotes[0] < REGRESSION_RECHECK):
                    n, matched, by_venue = (
                        cached_quotes[1], cached_quotes[2], cached_quotes[3])
                else:
                    quote_rows = conn.execute(
                        """SELECT f.venue,COUNT(*) AS n,
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
                             AND f.issued_at>=? AND f.issued_at<?
                           GROUP BY f.venue""",
                        ((stamp-timedelta(hours=2)).isoformat(),
                         (stamp-timedelta(minutes=5)).isoformat()),
                    ).fetchall()
                    n = sum(int(row["n"]) for row in quote_rows)
                    matched = sum(int(row["matched"] or 0) for row in quote_rows)
                    by_venue = ", ".join(
                        f'{row["venue"]} {int(row["matched"] or 0)}/{int(row["n"])}'
                        for row in quote_rows
                    )
                    self._quote_coverage_cache = (stamp, n, matched, by_venue)
                if n >= 12 and matched / n < 0.8:
                    faults["quote_coverage"] = (
                        f"{matched}/{n} prospective actionable family forecasts "
                        f"have a source-matched book (by venue: {by_venue})"
                    )
            cached = self._regression_cache
            if learning_enabled and cached is not None and (
                    timedelta(0) <= stamp - cached[0]
                    < (REGRESSION_RECHECK if regressions_inline else REGRESSION_STALE)):
                # A 7-day paired score barely moves in minutes; re-aggregating
                # the whole shadow ledger on every 2-minute watchdog pass was
                # one of the slow SELECTs. The alerts themselves are unchanged.
                faults.update(cached[1])
            elif learning_enabled and regressions_inline:
                faults.update(self._score_regressions(conn, stamp))
            elif learning_enabled:
                # No usable verdict (just restarted, or the aggregate has not
                # finished). Unknown is not clear: an open regression stays
                # open with its last verified detail instead of being closed
                # here and re-raised when the aggregate lands.
                faults.update({key: detail for key, detail in active.items()
                               if key.startswith("regression:")})
                if warm or "regression_check_stalled" in active:
                    faults["regression_check_stalled"] = (
                        "7-day challenger score check has not completed; "
                        "open regressions are carried forward unverified"
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

    def _score_regressions(self, conn, stamp: datetime) -> dict[str, str]:
        """Run the 7-day paired challenger aggregate and store its verdict."""
        regression_faults: dict[str, str] = {}
        in_use_since = (stamp-REGRESSION_IN_USE_WITHIN).isoformat()
        # One scan serves two readers. The alert below pools a family's
        # horizons per venue, as before. The early-stop rule (early_stop.py)
        # needs each horizon by itself plus the spread of the paired loss, so
        # the scan groups by horizon and the alert re-pools in Python.
        scores = [dict(row) for row in conn.execute(
            """SELECT venue,contract,quote,horizon_minutes,family,COUNT(*) AS n,
                      AVG(model_brier) AS model_score,
                      AVG(incumbent_brier) AS incumbent_score,
                      AVG((model_brier-incumbent_brier)*(model_brier-incumbent_brier))
                          AS loss_square,
                      COUNT(DISTINCT SUBSTR(target_opened_at,1,10)) AS days,
                      MAX(issued_at) AS last_issued
               FROM rival_model_shadow_forecasts
               WHERE settled_at IS NOT NULL AND issued_at>=?
                 AND model_brier IS NOT NULL AND incumbent_brier IS NOT NULL
               GROUP BY venue,contract,quote,horizon_minutes,family""",
            ((stamp-timedelta(days=7)).isoformat(),),
        ).fetchall()]
        pooled: dict[tuple[str, str, str, str], list] = {}
        for row in scores:
            count = int(row["n"])
            entry = pooled.setdefault(
                (row["venue"], row["contract"], row["quote"], row["family"]),
                [0, 0.0, 0.0, ""])
            entry[0] += count
            entry[1] += float(row["model_score"]) * count
            entry[2] += float(row["incumbent_score"]) * count
            entry[3] = max(entry[3], str(row["last_issued"]))
        for (venue, contract, _quote, family), (
                count, model_sum, incumbent_sum, last_issued) in sorted(pooled.items()):
            # Only families still issuing: a retired or replaced
            # challenger keeps its record but stops raising incidents.
            if count < 50 or last_issued < in_use_since:
                continue
            model_score, incumbent_score = model_sum / count, incumbent_sum / count
            if model_score > incumbent_score + 0.025:
                regression_faults[f"regression:{venue}:{contract}:{family}"] = (
                    f"paired Brier {model_score:.4f} vs "
                    f"incumbent {incumbent_score:.4f}, "
                    f"n={count}; promotion must stay gated"
                )
        self._regression_cache = (stamp, regression_faults)
        self._paired_scores = tuple(scores)
        self._paired_in_use_since = in_use_since
        return regression_faults

    def paired_scores(self) -> tuple[tuple[dict, ...], str | None]:
        """The last aggregate per stream and horizon, and its in-use cutoff."""
        return self._paired_scores, self._paired_in_use_since

    def regressions_due(self, now: datetime) -> bool:
        """True when the stored regression verdict is missing or due a recheck."""
        cached = self._regression_cache
        return cached is None or not (
            timedelta(0) <= now.astimezone(UTC) - cached[0] < REGRESSION_RECHECK)

    def refresh_regressions(self, *, now: datetime | None = None) -> dict[str, str]:
        """Recompute the regression verdict on its own connection.

        The watchdog runs this beside its pass, never inside it, so the pass
        stays point reads and cached verdicts. Read-only; a failure leaves
        the previous verdict in place and raises to the caller.
        """
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("regression check requires a UTC-aware clock")
        stamp = stamp.astimezone(UTC)
        with self.store.connect() as conn:
            return self._score_regressions(conn, stamp)

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
        if not self._boot_written:
            self.record_boot()
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
                if key.startswith("regression:"):
                    # A challenger losing to the incumbent by more than the
                    # promotion bar is the gate working as designed (RIVAL-F:
                    # liq_v1 phemex:ETH), not a fault in the host, the loop
                    # or the model code. It still reaches Sentry, but as a
                    # warning under its own fingerprint, so real faults stand
                    # out — see observability.report_model_quality. Logging
                    # it at error here would ALSO create a second,
                    # error-level Sentry event via the logging integration.
                    # The local problem log keeps WARNING+ lines, so the
                    # alert still shows on the status card, as "Warning: ".
                    _LOG.warning("RivaL HEALTH ALERT %s: %s", key, detail)
                    report_model_quality(key, detail)
                else:
                    with health_alert_scope(key):
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
            boot = conn.execute(
                "SELECT boot_id,started_at FROM rival_runtime_health_boots ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return {
            "checked_at": pulse["checked_at"] if pulse else None,
            "boot_id": boot["boot_id"] if boot else None,
            "boot_started_at": boot["started_at"] if boot else None,
            "issues": [dict(r) for r in rows],
        }
