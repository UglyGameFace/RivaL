from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _store(tmp_path, *, forecast_age, trained_age, review_age, pooled_scores):
    store = SQLiteHotStore(tmp_path / "alerts.sqlite")
    with store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS perp_forward_audit (issued_at TEXT)")
        conn.execute("INSERT INTO perp_forward_audit VALUES (?)",
                     ((NOW - forecast_age).isoformat(),))
        conn.execute("""CREATE TABLE rival_pooled_models (trained_through TEXT,
                        gates_passed INTEGER)""")
        conn.execute("INSERT INTO rival_pooled_models VALUES (?,1)",
                     (str(NOW - trained_age).replace("T", " "),))
        conn.execute("CREATE TABLE rival_intelligence_calls (issued_at TEXT)")
        conn.execute("INSERT INTO rival_intelligence_calls VALUES (?)",
                     ((NOW - review_age).isoformat(),))
        conn.execute("""CREATE TABLE IF NOT EXISTS rival_model_shadow_forecasts (
                        family TEXT, horizon_minutes INTEGER, settled_at TEXT,
                        model_brier REAL, baseline_brier REAL)""")
        for model, base in pooled_scores:
            conn.execute(
                "INSERT INTO rival_model_shadow_forecasts VALUES (?,?,?,?,?)",
                ("pooled_5m_abc", 5, (NOW - timedelta(hours=1)).isoformat(), model, base))
    return store


def test_healthy_prediction_pipeline_raises_no_alerts(tmp_path):
    store = _store(tmp_path, forecast_age=timedelta(minutes=3),
                   trained_age=timedelta(days=2), review_age=timedelta(minutes=10),
                   pooled_scores=[(.24, .25)] * 400)
    try:
        assert research_alerts(store, now=NOW) == []
    finally:
        store.close()


def test_every_failure_mode_is_reported(tmp_path):
    store = _store(tmp_path, forecast_age=timedelta(hours=1),
                   trained_age=timedelta(days=5), review_age=timedelta(hours=6),
                   pooled_scores=[(.26, .25)] * 400)
    try:
        keys = {key for key, _ in research_alerts(store, now=NOW)}
        assert keys == {"forecasts:stopped", "pooled:stale", "decay:5m",
                        "intelligence:stale"}
        # Intelligence switched off: no alert for it.
        keys = {key for key, _ in research_alerts(store, now=NOW,
                                                   intelligence_enabled=False)}
        assert "intelligence:stale" not in keys
    finally:
        store.close()


def test_decay_needs_enough_graded_forecasts(tmp_path):
    store = _store(tmp_path, forecast_age=timedelta(minutes=3),
                   trained_age=timedelta(days=1), review_age=timedelta(minutes=5),
                   pooled_scores=[(.30, .25)] * 50)
    try:
        assert research_alerts(store, now=NOW) == []
    finally:
        store.close()
