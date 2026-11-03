"""Prediction-health alerts for the status card (read-only database checks).

Each alert is (stable key, text). Keys never contain counters, so the card
re-posts only when the SET of problems changes. Every check fails soft: an
unreadable table becomes its own alert instead of a guess.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

FORECASTS_STALE_AFTER = timedelta(minutes=20)  # cycles run every 5 minutes
MODEL_STALE_AFTER = timedelta(days=3)  # nightly training; 3 misses in a row
INTELLIGENCE_STALE_AFTER = timedelta(hours=2)  # reviews every 5-15 minutes
DECAY_WINDOW = timedelta(days=3)
DECAY_MIN_GRADED = 300


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
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    alerts: list[tuple[str, str]] = []

    def scalar(sql: str, params=()):
        with store.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # 1. Forecast cycles are still issuing.
    try:
        newest = _utc(scalar("SELECT MAX(issued_at) FROM perp_forward_audit"))
        if newest is None or stamp - newest > FORECASTS_STALE_AFTER:
            alerts.append(("forecasts:stopped",
                           "🛑 No new forecasts for "
                           + (_ago(stamp - newest) if newest else "ever")
                           + " (cycles run every 5 min)"))
    except Exception:  # noqa: BLE001
        _LOG.warning("Research alert: forecast table unreadable")
        alerts.append(("forecasts:unreadable", "⚠️ Forecast table unreadable"))

    # 2. Nightly six-year model is fresh (training ran AND passed its gates).
    try:
        trained = _utc(scalar(
            "SELECT MAX(trained_through) FROM rival_pooled_models WHERE gates_passed=1"))
        if trained is None:
            alerts.append(("pooled:none", "⚠️ No six-year model imported yet"))
        elif stamp - trained > MODEL_STALE_AFTER:
            alerts.append(("pooled:stale",
                           (f"⚠️ Six-year model data is {_ago(stamp - trained)} old: "
                            "nightly training failed or missed its accuracy gates")))
    except Exception:  # noqa: BLE001 - table appears after the first import
        alerts.append(("pooled:none", "⚠️ No six-year model imported yet"))

    # 3. Live accuracy decay: the six-year model worse than the base rate.
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT horizon_minutes, COUNT(*) AS n,
                          AVG(model_brier) AS m, AVG(baseline_brier) AS b
                   FROM rival_model_shadow_forecasts
                   WHERE family LIKE 'pooled%' AND settled_at IS NOT NULL
                     AND model_brier IS NOT NULL AND baseline_brier IS NOT NULL
                     AND settled_at>=?
                   GROUP BY horizon_minutes""",
                ((stamp - DECAY_WINDOW).isoformat(),),
            ).fetchall()
        for row in rows:
            if int(row["n"]) >= DECAY_MIN_GRADED and float(row["m"]) > float(row["b"]):
                horizon = int(row["horizon_minutes"])
                alerts.append((f"decay:{horizon}m",
                               (f"⚠️ {horizon}-min six-year model scored worse than the "
                                f"plain up-rate over the last 3 days ({int(row['n'])} graded)")))
    except Exception:  # noqa: BLE001
        _LOG.warning("Research alert: challenger table unreadable")

    # 4. RivaL Intelligence is still producing reviews.
    if intelligence_enabled:
        try:
            newest = _utc(scalar("SELECT MAX(issued_at) FROM rival_intelligence_calls"))
            if newest is None or stamp - newest > INTELLIGENCE_STALE_AFTER:
                alerts.append(("intelligence:stale",
                               "⚠️ RivaL Intelligence: no review for "
                               + (_ago(stamp - newest) if newest else "ever")))
        except Exception:  # noqa: BLE001 - table appears with the first review
            alerts.append(("intelligence:stale", "⚠️ RivaL Intelligence: no review yet"))
    return alerts
