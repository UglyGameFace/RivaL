from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _store(tmp_path, *, forecast_age, trained_age, review_age, pooled_scores):
    store = SQLiteHotStore(tmp_path / "alerts.sqlite")
    with store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS perp_forward_audit "
                     "(asset TEXT, horizon_minutes INTEGER, issued_at TEXT)")
        conn.execute("INSERT INTO perp_forward_audit VALUES ('BTC', 5, ?)",
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


def test_unreadable_tables_say_so_instead_of_claiming_missing(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")  # no tables at all
    try:
        keys = {key for key, _ in research_alerts(store, now=NOW)}
        assert keys == {"forecasts:unreadable", "pooled:unreadable",
                        "intelligence:unreadable"}
    finally:
        store.close()


def test_busy_pool_gives_one_honest_line_not_three(tmp_path):
    class BusyStore:
        calls = 0

        def connect(self):
            BusyStore.calls += 1
            raise TimeoutError("all pooled connections are checked out")

    alerts = research_alerts(BusyStore(), now=NOW)
    assert [key for key, _ in alerts] == ["database:busy"]
    assert BusyStore.calls == 1  # one checkout attempt, not one per check


def test_checks_share_a_single_connection(tmp_path):
    store = _store(tmp_path, forecast_age=timedelta(minutes=1),
                   trained_age=timedelta(hours=12), review_age=timedelta(minutes=5),
                   pooled_scores=[])
    opened = []
    real = store.connect

    def counting():
        opened.append(1)
        return real()

    store.connect = counting
    try:
        assert research_alerts(store, now=NOW) == []
        assert len(opened) == 1
    finally:
        store.close()


def test_checks_work_with_name_keyed_rows_like_postgres(tmp_path):
    store = _store(tmp_path, forecast_age=timedelta(minutes=1),
                   trained_age=timedelta(hours=12), review_age=timedelta(minutes=5),
                   pooled_scores=[])
    real = store.connect

    class NameKeyed:  # PostgreSQL dict rows: row[0] raises KeyError
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)

            class Rows:
                def fetchone(self):
                    row = cursor.fetchone()
                    return None if row is None else dict(row)

                def fetchall(self):
                    return [dict(r) for r in cursor.fetchall()]
            return Rows()

    from contextlib import contextmanager

    @contextmanager
    def connect():
        with real() as conn:
            yield NameKeyed(conn)

    store.connect = connect
    try:
        assert research_alerts(store, now=NOW) == []
    finally:
        store.close()


def test_month_complete_training_data_is_not_called_stale_mid_month(tmp_path):
    # Data through the 1st is exactly what monthly training produces, even on the 25th.
    late_month = datetime(2026, 11, 25, 12, tzinfo=UTC)
    store = _store(tmp_path, forecast_age=timedelta(minutes=1),
                   trained_age=late_month - datetime(2026, 11, 1, tzinfo=UTC),
                   review_age=timedelta(minutes=5), pooled_scores=[])
    try:
        # _store stamps rows relative to NOW; re-stamp them for late_month.
        with store.connect() as conn:
            conn.execute("UPDATE perp_forward_audit SET issued_at=?",
                         ((late_month - timedelta(minutes=1)).isoformat(),))
            conn.execute("UPDATE rival_intelligence_calls SET issued_at=?",
                         ((late_month - timedelta(minutes=5)).isoformat(),))
            conn.execute("UPDATE rival_pooled_models SET trained_through='2026-11-01 00:00:00+00:00'")
        assert research_alerts(store, now=late_month) == []
        with store.connect() as conn:  # still on October data on the 25th: stale
            conn.execute("UPDATE rival_pooled_models SET trained_through='2026-10-01 00:00:00+00:00'")
        assert [k for k, _ in research_alerts(store, now=late_month)] == ["pooled:stale"]
    finally:
        store.close()
