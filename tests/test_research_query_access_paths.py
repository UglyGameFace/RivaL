"""Regression for the DB access paths behind RivaL's repeated slow-query alerts.

These tests cover the exact predicates of the live status/read-only readers:
multi-family 7-day hit rates, 14-day forward summaries, and review-to-forecast
target joins. No market history is rewritten or inference/promotion changed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def _prepare(tmp_path):
    store = SQLiteHotStore(tmp_path / "query-access.sqlite")
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    return store, monitor


def _columns(conn, name):
    return [row["name"] for row in conn.execute(
        f"PRAGMA index_info({name})").fetchall()]


def test_hot_query_indexes_are_exact_and_reentrant(tmp_path):
    store, monitor = _prepare(tmp_path)
    try:
        with store.connect() as conn:
            assert _columns(conn, "idx_rival_model_shadow_recent_hit") == [
                "asset", "horizon_minutes", "issued_at", "family"]
            assert _columns(conn, "idx_rival_model_shadow_recent_hit_cover") == [
                "asset", "horizon_minutes", "issued_at", "family",
                "venue", "probability_up", "outcome_up"]
            assert _columns(conn, "idx_perp_forward_score_recent") == [
                "asset", "model_version", "issued_at", "horizon_minutes"]
            assert _columns(conn, "idx_perp_forward_intel_target") == [
                "asset", "horizon_minutes", "target_opened_at", "model_version",
                "issued_at"]
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                ("idx_rival_model_shadow_recent_hit",),
            ).fetchone()["sql"]
            assert "WHERE settled_at IS NOT NULL" in sql
            assert "outcome_up IN (0,1)" in sql
            cover_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                ("idx_rival_model_shadow_recent_hit_cover",),
            ).fetchone()["sql"]
            assert "WHERE settled_at IS NOT NULL" in cover_sql
            assert "outcome_up IN (0,1)" in cover_sql
        monitor._initialize_schema()  # startup DDL is idempotent
        with store.connect() as conn:
            assert _columns(conn, "idx_perp_forward_intel_target")[2] == (
                "target_opened_at")
    finally:
        store.close()


def test_hot_queries_use_time_bound_and_target_indexes(tmp_path):
    store, _ = _prepare(tmp_path)
    now = datetime(2026, 11, 12, 15, tzinfo=UTC)
    since = (now - timedelta(days=7)).isoformat()
    try:
        with store.connect() as conn:
            hit_plan = conn.execute(
                """EXPLAIN QUERY PLAN SELECT family, COUNT(*) AS n,
                          SUM(CASE WHEN (probability_up>0.5 AND outcome_up=1)
                                     OR (probability_up<0.5 AND outcome_up=0)
                                   THEN 1 ELSE 0 END) AS came_true
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND issued_at>=?
                     AND settled_at IS NOT NULL AND outcome_up IN (0,1)
                     AND probability_up BETWEEN 0 AND 1 AND probability_up<>0.5
                   GROUP BY family""",
                ("BTC", 5, since),
            ).fetchall()
            summary_plan = conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT horizon_minutes,COUNT(*) FROM perp_forward_audit
                   WHERE asset=? AND model_version=? AND issued_at>=?
                     AND issued_at<=? GROUP BY horizon_minutes""",
                ("BTC", MODEL, since, now.isoformat()),
            ).fetchall()
            review_plan = conn.execute(
                """EXPLAIN QUERY PLAN SELECT issued_at FROM perp_forward_audit
                   WHERE asset=? AND horizon_minutes=? AND target_opened_at=?
                     AND model_version=? AND issued_at<=?""",
                ("BTC", 5, now.isoformat(), MODEL, now.isoformat()),
            ).fetchall()
        assert "idx_rival_model_shadow_recent_hit" in " ".join(
            str(row["detail"]) for row in hit_plan)
        assert "idx_perp_forward_score_recent" in " ".join(
            str(row["detail"]) for row in summary_plan)
        assert "idx_perp_forward_intel_target" in " ".join(
            str(row["detail"]) for row in review_plan)
    finally:
        store.close()


def test_model_lab_scored_review_uses_targeted_partial_index(tmp_path, monkeypatch):
    """Exercise the exact PostgreSQL slow-query shape, not copied SQL.

    The four-family, four-asset review used to spend 3-5 seconds on each
    scan of the full shadow ledger. Its partial access path must remain
    selective for both daily target gains and distinct scored streams,
    which now arrive from one shared window scan.
    """
    from parlay_bot.perpetuals.model_lab import ModelLab
    from parlay_bot.storage import optional_reads

    store, monitor = _prepare(tmp_path)
    captured = []
    original = optional_reads.optional_rows

    def record_read(database, sql, params=(), *, what="optional read"):
        if "FROM rival_model_shadow_forecasts" in sql:
            captured.append((sql, tuple(params)))
        return original(database, sql, params, what=what)

    monkeypatch.setattr(optional_reads, "optional_rows", record_read)
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    since = (now - timedelta(days=21)).isoformat()
    families = [f"lab_5m_review_{i}" for i in range(4)]
    try:
        scores = ModelLab(store)._scores(families, 5, since, through=now)
        assert scores is not None and set(scores) == set(families)
        assert len(captured) == 1
        with store.connect() as conn:
            assert _columns(conn, "idx_rival_model_shadow_lab_scored") == [
                "horizon_minutes", "family", "asset", "issued_at"
            ]
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                ("idx_rival_model_shadow_lab_scored",),
            ).fetchone()["sql"]
            assert "settled_at IS NOT NULL" in definition
            assert "model_brier BETWEEN 0 AND 1" in definition
            assert "incumbent_brier BETWEEN 0 AND 1" in definition
            # The cover twin carries every column the score read evaluates,
            # so PostgreSQL can answer it index-only instead of fetching
            # each of the ~260k window rows from the heap (7-11 s per
            # horizon on production, 2026-11-28).
            assert _columns(conn, "idx_rival_model_shadow_lab_scored_cover") == [
                "horizon_minutes", "family", "asset", "issued_at",
                "target_opened_at", "venue", "contract", "settled_at",
                "model_brier", "incumbent_brier"
            ]
            cover_definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                ("idx_rival_model_shadow_lab_scored_cover",),
            ).fetchone()["sql"]
            assert "settled_at IS NOT NULL" in cover_definition
            assert "model_brier BETWEEN 0 AND 1" in cover_definition
            assert "incumbent_brier BETWEEN 0 AND 1" in cover_definition
            for sql, params in captured:
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, params,
                ).fetchall()
                assert "idx_rival_model_shadow_lab_scored" in " ".join(
                    str(row["detail"]) for row in plan
                ), (sql, [str(row["detail"]) for row in plan])
        # Existing startup DDL must not duplicate or invalidate the index.
        monitor._initialize_schema()
        with store.connect() as conn:
            assert _columns(conn, "idx_rival_model_shadow_lab_scored")[0] == (
                "horizon_minutes"
            )
    finally:
        store.close()


def test_live_calibration_reuses_one_bounded_read_without_caching_historical_clocks(
    tmp_path, monkeypatch,
):
    store, monitor = _prepare(tmp_path)
    calls = []

    def fake(*, asset, horizon, now):
        calls.append((asset, horizon, now))
        return {"bands": [], "regimes": {}, "window_days": 7}

    monkeypatch.setattr(monitor, "_calibration_uncached", fake)
    try:
        result = monitor.calibration(asset="BTC", horizon=5)
        assert monitor.calibration(asset="BTC", horizon=5) == result
        assert len(calls) == 1
        historical = datetime(2025, 1, 1, tzinfo=UTC)
        assert monitor.calibration(asset="BTC", horizon=5, now=historical) == result
        assert calls[-1][2] == historical
        assert len(calls) == 2
    finally:
        store.close()
