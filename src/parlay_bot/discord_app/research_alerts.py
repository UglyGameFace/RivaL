"""Prediction-health alerts for the status card (read-only database checks).

Each alert is (stable key, text). Keys never contain counters, so the card
re-posts only when the SET of problems changes. Every check fails soft: an
unreadable table becomes its own alert instead of a guess.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

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
        with store.connect() as conn:
            return _checks(conn, stamp, intelligence_enabled)
    except Exception as exc:  # noqa: BLE001 - pool checkout, not a missing table
        _LOG.warning("Research alert: no database connection for health checks (%s)",
                     type(exc).__name__)
        return [("database:busy",
                 "⚠️ Couldn't check the predictions (database busy); will try again")]


def _checks(conn, stamp: datetime, intelligence_enabled: bool) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []

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
    return alerts
