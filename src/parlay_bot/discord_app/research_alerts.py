"""Prediction-health alerts for the status card (read-only database checks).

Each alert is (stable key, text). Keys never contain counters, so the card
re-posts only when the SET of problems changes. Every check fails soft: an
unreadable table becomes its own alert instead of a guess.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.promotion_receipts import initialize as _init_receipts
from parlay_bot.perpetuals.research_loop import initialize as _init_experiments

_LOG = logging.getLogger(__name__)

FORECASTS_STALE_AFTER = timedelta(minutes=20)  # cycles run every 5 minutes
# Training uses COMPLETE calendar months only (the workflow passes the last
# finished month), so its data always ends on the 1st of a month. Judging it
# by "older than 3 days" fired falsely from the 4th of every month. It is
# stale only when this month's 1st is still missing after a short grace.
MONTH_GRACE = timedelta(days=2)
INTELLIGENCE_STALE_AFTER = timedelta(hours=2)  # reviews every 5-15 minutes
DECAY_WINDOW = timedelta(days=3)
DECAY_MIN_GRADED = 300
DECAY_SIGMAS = 2.0
# A trained version counts as in use if it issued a forecast this recently.
IN_USE_WITHIN = timedelta(minutes=30)


def _utc(value) -> datetime | None:
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace(" ", "T", 1))
    except ValueError:
        return None
    return when if when.utcoffset() is not None else when.replace(tzinfo=UTC)


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours * 60:.0f} min" if hours < 2 else f"{hours:.0f} h" if hours < 48 \
        else f"{hours / 24:.0f} days"


def research_alerts(store, *, now: datetime | None = None,
                    intelligence_enabled: bool = True) -> list[tuple[str, str]]:
    """All checks share ONE pooled connection.

    Each check used to check out its own connection. When the pool was busy,
    every checkout waited up to its timeout separately, the card's 15 s limit
    ran out, and three "database busy" lines appeared even though each query
    takes milliseconds. Now a busy pool gives a single honest line.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        # The receipts and research-memory tables are created lazily (only
        # on a first promotion / first experiment), but checks 5 and 6 read
        # them on every render. Create them here, idempotently
        # (CREATE TABLE IF NOT EXISTS), so a render before the first
        # promotion does not warn about unreadable tables — and, on
        # Postgres, does not abort the shared transaction for check 6.
        # Inside this try on purpose: a pool checkout failure here is the
        # same "database:busy" the shared connection reports below.
        _init_receipts(store)
        _init_experiments(store)
        with store.connect() as conn:
            return _checks(conn, stamp, intelligence_enabled)
    except Exception as exc:  # noqa: BLE001 - pool checkout, not a missing table
        _LOG.warning("Research alert: no database connection for health checks (%s)",
                     type(exc).__name__)
        return [("database:busy",
                 "⚠️ Couldn't check the predictions (database busy); will try again")]


def _checks(conn, stamp: datetime, intelligence_enabled: bool) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []

    # Check 8 reads the promotion evaluation table and the blocked-run
    # receipts table on every render. Create both on this same shared
    # connection (idempotent CREATE TABLE IF NOT EXISTS, plain execute so
    # name-keyed row wrappers work) so a render before the first
    # evaluation logs no "unreadable" warning — and the card still checks
    # out exactly one shared connection. A failure here falls through to
    # check 8's own unreadable alert instead of failing the whole card.
    try:
        from parlay_bot.perpetuals.family_governance import (
            FAMILY_PROMOTION_SCHEMA,
        )
        from parlay_bot.perpetuals.research_hypotheses import (
            BLOCKED_REPORTS_SCHEMA,
        )

        conn.execute(FAMILY_PROMOTION_SCHEMA)
        conn.execute(BLOCKED_REPORTS_SCHEMA)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Research alert: check-8 tables unreadable (%s)",
                     type(exc).__name__)

    def scalar(sql: str, params=()):
        row = conn.execute(sql, params).fetchone()
        # PostgreSQL rows are keyed by column name (row[0] raises KeyError,
        # which made every check read as "unreadable"); alias the one value.
        return None if row is None else row["v"]

    # 1. Forecast cycles are still issuing.
    try:
        # Indexed lookup (asset, horizon_minutes, issued_at); BTC 5-min is
        # issued every cycle, so it is the cycle's own heartbeat.
        newest = _utc(scalar("SELECT MAX(issued_at) AS v FROM perp_forward_audit "
                             "WHERE asset='BTC' AND horizon_minutes=5"))
        if newest is None or stamp - newest > FORECASTS_STALE_AFTER:
            alerts.append(("forecasts:stopped",
                           "🛑 No new predictions for "
                           + (_ago(stamp - newest) if newest else "ever")
                           + " (they should come every 5 min)"))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Research alert: forecast table unreadable (%s)", type(exc).__name__)
        alerts.append(("forecasts:unreadable",
                       "⚠️ Couldn't read the predictions; will try again"))

    # 2. Nightly six-year model is fresh (training ran AND passed its gates).
    try:
        trained = _utc(scalar(
            "SELECT MAX(trained_through) AS v FROM rival_pooled_models WHERE gates_passed=1"))
        if trained is None:
            alerts.append(("pooled:none", "⚠️ The 6-year model hasn't been loaded yet"))
        else:
            month_start = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if trained < month_start and stamp - month_start > MONTH_GRACE:
                alerts.append(("pooled:stale",
                               (f"⚠️ The 6-year model's data stops at {trained:%b %d}: "
                                f"{month_start:%B}'s monthly update hasn't been loaded "
                                "(training failed, didn't pass its checks, or didn't run)")))
    except Exception as exc:  # noqa: BLE001 - unreadable is not the same as missing
        _LOG.warning("Research alert: six-year model table unreadable (%s)",
                     type(exc).__name__)
        alerts.append(("pooled:unreadable",
                       "⚠️ Couldn't read the 6-year model; will try again"))

    # 3. Live accuracy decay: a six-year model IN USE worse than the base rate.
    # Judged per trained version, and only for versions issued in the last
    # IN_USE_WITHIN: averaging a family over 3 days kept alerting on versions
    # already replaced by nightly retraining. A version with fewer than
    # DECAY_MIN_GRADED graded results gets no verdict yet.
    try:
        rows = conn.execute(
            """SELECT horizon_minutes, family, SUBSTR(training_sha256,1,12) AS model,
                      COUNT(*) AS n, AVG(model_brier) AS m, AVG(baseline_brier) AS b,
                      AVG((model_brier-baseline_brier)*(model_brier-baseline_brier)) AS d2,
                      COUNT(DISTINCT target_opened_at) AS targets,
                      MAX(issued_at) AS last_issued
               FROM rival_model_shadow_forecasts
               WHERE family LIKE 'pooled%' AND settled_at IS NOT NULL
                 AND model_brier IS NOT NULL AND baseline_brier IS NOT NULL
                 AND settled_at>=?
               GROUP BY horizon_minutes, family, SUBSTR(training_sha256,1,12)""",
            ((stamp - DECAY_WINDOW).isoformat(),),
        ).fetchall()
        worst: dict[int, tuple] = {}
        for row in rows:
            last = _utc(row["last_issued"])
            if (last is None or stamp - last > IN_USE_WITHIN
                    or int(row["n"]) < DECAY_MIN_GRADED):
                continue
            gap = float(row["m"]) - float(row["b"])
            # Alert only when the gap is clearly beyond chance: more than
            # DECAY_SIGMAS standard errors of the per-forecast differences.
            # Venues share targets (correlated), so the error uses distinct
            # targets, not rows. On Nov 5 a +0.0009 gap (pure noise; the same
            # version had been ahead hours earlier) kept the card Degraded.
            variance = max(0.0, float(row["d2"] or 0) - gap * gap)
            targets = max(1, int(row["targets"] or 0))
            error = math.sqrt(variance / targets)
            if gap <= DECAY_SIGMAS * error:
                continue
            horizon = int(row["horizon_minutes"])
            if horizon not in worst or gap > worst[horizon][0]:
                worst[horizon] = (gap, str(row["model"]), int(row["n"]), gap / error if error else 0.0)
        for horizon, (gap, model, n, sigmas) in sorted(worst.items()):
            alerts.append((f"decay:{horizon}m",
                           (f"⚠️ The {horizon}-min 6-year model has done worse than a "
                            f"simple guess over the last 3 days ({n:,} results"
                            + (f"; the gap is {sigmas:.1f}× bigger than chance would explain"
                               if sigmas > 0 else "")
                            + f"). Version {model}, error-score gap +{gap:.4f}")))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Research alert: challenger table unreadable (%s)", type(exc).__name__)

    # 4. RivaL Intelligence is still producing reviews.
    if intelligence_enabled:
        try:
            newest = _utc(scalar("SELECT MAX(issued_at) AS v FROM rival_intelligence_calls"))
            if newest is None or stamp - newest > INTELLIGENCE_STALE_AFTER:
                alerts.append(("intelligence:stale",
                               "⚠️ RivaL Intelligence hasn't reviewed anything for "
                               + (_ago(stamp - newest) if newest else "ever")))
        except Exception as exc:  # noqa: BLE001 - unreadable is not the same as missing
            _LOG.warning("Research alert: Intelligence table unreadable (%s)",
                         type(exc).__name__)
            alerts.append(("intelligence:unreadable",
                           "⚠️ Couldn't read RivaL Intelligence's reviews; will try again"))

    # 5. flow_v3 / liq_v1 / archive_v1 promotion receipts (batch 2, spec §2.7;
    # Phase 3 adds liq_v1; autonomous-loop D18 adds archive_v1, closing the
    # Phase 2 carryover): the first promoted_paper write and first serve for a
    # family + stream are Discord receipts, delivered through this card (it
    # posts to rival_status_channel_id when its alert set changes). A
    # missing receipts table means no promotion happened, not an alert.
    try:
        rows = conn.execute(
            """SELECT family,venue,contract,quote,horizon_minutes,kind,receipt_text
               FROM rival_family_promotion_receipts
               WHERE family IN ('flow_v3','liq_v1','archive_v1') AND recorded_at>=?
               ORDER BY recorded_at""",
            ((stamp - timedelta(hours=48)).isoformat(),),
        ).fetchall()
        for row in rows:
            key = (f"{row['family'].replace('_', '-')}:{row['kind']}:{row['venue']}:"
                   f"{row['contract']}:{row['horizon_minutes']}m")
            alerts.append((key, str(row["receipt_text"])))
    except Exception as exc:  # noqa: BLE001 - receipts must never break the card
        _LOG.warning("Research alert: promotion receipts unreadable (%s)", type(exc).__name__)

    # 6. Research-loop experiments (batch 2 Phase 3, spec §3.1-3.2): a
    # recently promoted experiment's discovery finding, surfaced through
    # the same status-card pattern as the promotion receipts. Only
    # promoted experiments appear here; discarded/failed ones stay in
    # the tables as the mistakes memory, not as card noise. A missing
    # table means no experiment promoted, not an alert.
    try:
        rows = conn.execute(
            """SELECT e.experiment_id, e.hypothesis, f.summary
               FROM rival_research_experiments e
               LEFT JOIN rival_research_findings f
                 ON f.experiment_id = e.experiment_id AND f.kind='discovery'
               WHERE e.status='promoted' AND e.created_at>=?
               ORDER BY e.created_at""",
            ((stamp - timedelta(hours=48)).isoformat(),),
        ).fetchall()
        for row in rows:
            key = f"research-experiment:promoted:{row['experiment_id']}"
            summary = str(row["summary"] or "promoted on the forward shadow record")
            alerts.append((key, (f"🔬 Research experiment promoted "
                                 f"({row['hypothesis']}): {summary}")))
    except Exception as exc:  # noqa: BLE001 - experiments must never break the card
        _LOG.warning("Research alert: research experiments unreadable (%s)",
                     type(exc).__name__)

    # 7. Research-loop driver counts (autonomous loop Phase 1, spec §2.5):
    # open experiments and outcomes closed in the last 7 days, counts
    # only. A closed experiment counts by the time its terminal
    # transition was recorded (the outcome_json event log), not by
    # creation time. Discarded/failed are never surfaced as wins; the
    # line appears only when there is loop activity to report, and its
    # key carries no counters so the card does not re-post on count
    # changes alone.
    try:
        rows = conn.execute(
            """SELECT status, outcome_json FROM rival_research_experiments
               WHERE created_at>=?""",
            ((stamp - timedelta(days=90)).isoformat(),),
        ).fetchall()
        open_count = 0
        closed = {"promoted": 0, "discarded": 0, "failed": 0}
        for row in rows:
            status = str(row["status"])
            if status not in closed:
                open_count += 1
                continue
            try:
                events = json.loads(row["outcome_json"] or "[]")
            except (ValueError, TypeError):
                continue
            closed_at = None
            for event in events or []:
                if isinstance(event, dict) and event.get("stage") == status:
                    closed_at = _utc(event.get("at"))
            if closed_at is not None and stamp - closed_at <= timedelta(days=7):
                closed[status] += 1
        if open_count or any(closed.values()):
            alerts.append(("research-loop:counts",
                           (f"🔬 Research loop: {open_count} open experiment(s); "
                            f"last 7 days: {closed['promoted']} promoted, "
                            f"{closed['discarded']} discarded, "
                            f"{closed['failed']} failed")))
    except Exception as exc:  # noqa: BLE001 - counts must never break the card
        _LOG.warning("Research alert: research loop counts unreadable (%s)",
                     type(exc).__name__)

    # 8. Per-family shadow progress + research blockers (autonomous loop
    # D18): days/targets against the real Route B thresholds, read from
    # the latest rival_family_promotion evaluation per (family, asset,
    # horizon); and the latest blocked-run receipt per venue. Qualified
    # families are not shown here — their promotion receipt (check 5)
    # already announces them. Progress keys carry no counters, so the
    # card re-posts only when the set of families being tracked changes.
    try:
        from parlay_bot.perpetuals.family_governance import MIN_DAYS, MIN_WINDOWS
    except Exception:  # noqa: BLE001 - thresholds are stable; fall back
        MIN_DAYS, MIN_WINDOWS = 21, 150
    try:
        rows = conn.execute(
            """SELECT asset, horizon_minutes, evidence_json
               FROM rival_family_promotion WHERE evaluated_at>=?
               ORDER BY evaluated_at DESC""",
            ((stamp - timedelta(hours=48)).isoformat(),),
        ).fetchall()
        best: dict[tuple[str, str, int], tuple[int, int]] = {}
        for row in rows:
            try:
                families = json.loads(row["evidence_json"] or "{}")
            except (ValueError, TypeError):
                continue
            if not isinstance(families, dict):
                continue
            for family, info in families.items():
                if not isinstance(info, dict) or info.get("qualified"):
                    continue
                targets, days = info.get("targets"), info.get("days")
                if not isinstance(targets, int) or not isinstance(days, int):
                    continue
                key = (str(family), str(row["asset"]),
                       int(row["horizon_minutes"]))
                if key not in best:
                    best[key] = (days, targets)
        for (family, asset, horizon), (days, targets) in sorted(best.items())[:12]:
            alerts.append(
                (f"shadow-progress:{family}:{asset}:{horizon}m",
                 (f"📊 {family} {asset} {horizon}m: {days}/{MIN_DAYS} days · "
                  f"{targets}/{MIN_WINDOWS} windows")))
    except Exception as exc:  # noqa: BLE001 - progress must never break the card
        _LOG.warning("Research alert: shadow progress unreadable (%s)",
                     type(exc).__name__)
    try:
        rows = conn.execute(
            """SELECT venue, missing_months_json, recorded_at
               FROM rival_research_blocked_reports WHERE recorded_at>=?
               ORDER BY recorded_at DESC""",
            ((stamp - timedelta(days=7)).isoformat(),),
        ).fetchall()
        latest: dict[str, dict] = {}
        for row in rows:
            if str(row["venue"]) not in latest:
                latest[str(row["venue"])] = {
                    "missing_months_json": row["missing_months_json"],
                }
        for venue, blocked in sorted(latest.items()):
            try:
                missing = json.loads(blocked["missing_months_json"] or "[]")
            except (ValueError, TypeError):
                missing = []
            missing = [str(m) for m in missing if isinstance(m, str)]
            shown = ", ".join(missing[:3])
            if len(missing) > 3:
                shown += ", …"
            alerts.append(
                (f"research-blocked:{venue}",
                 (f"⛔ Research blocked at {venue}: {len(missing)} month(s) "
                  f"missing ({shown})")))
    except Exception as exc:  # noqa: BLE001 - blockers must never break the card
        _LOG.warning("Research alert: research blockers unreadable (%s)",
                     type(exc).__name__)
    return alerts
