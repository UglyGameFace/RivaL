"""Owner-approved prediction cleanup: exactly what was approved, nothing else."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import retention
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 12, 30, tzinfo=UTC)


def _row(conn, family, *, issued, settled=True, ref=0):
    target = issued + timedelta(minutes=4)
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
             horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
             probability_up,incumbent_up,baseline_up,training_count,training_sha256,
             settled_at,outcome_up,model_brier,incumbent_brier)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("okx", "BTC-USDT-SWAP", "USDT", "BTC", 5, family,
         (issued - timedelta(minutes=1, seconds=ref)).isoformat(), issued.isoformat(),
         target.isoformat(), .6, .5, .5, 1, "a" * 64,
         (target + timedelta(minutes=6)).isoformat() if settled else None,
         1 if settled else None, .16 if settled else None, .25 if settled else None))


def _families(store):
    with store.connect() as conn:
        return sorted((r["family"], r["n"]) for r in conn.execute(
            "SELECT family, COUNT(*) AS n FROM rival_model_shadow_forecasts GROUP BY family"))


def test_cleanup_deletes_retired_ungradable_and_old_rows_only(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "r.sqlite")
    monkeypatch.setattr(retention, "BATCH", 2)   # exercise the batching
    try:
        ShadowModelCompetition(store).initialize()
        recent = NOW - timedelta(days=3)
        with store.connect() as conn:
            for i, family in enumerate(("liq_v1", "flow_v2", "pooled_5m_1273de73aa15_venue",
                                        "pooled_5m_dea81c66b992")):
                for k in range(3):
                    _row(conn, family, issued=recent, ref=10 * i + k)
            for k in range(3):
                _row(conn, "regime_v1", issued=recent, ref=100 + k)        # kept
                _row(conn, "pooled_5m_1273de73aa15", issued=recent, ref=200 + k)  # kept
            _row(conn, "knn21_v1", issued=NOW - timedelta(days=20), settled=False)  # ungradable
            _row(conn, "knn21_v1", issued=NOW - timedelta(days=50), ref=1)          # > 45 days
            _row(conn, "knn21_v1", issued=NOW - timedelta(days=40), ref=2)          # kept
            _row(conn, "knn21_v1", issued=NOW - timedelta(days=2), settled=False, ref=3)  # kept
            conn.execute("CREATE TABLE rival_liq1_features (issued_at TEXT)")
            for _ in range(5):
                conn.execute("INSERT INTO rival_liq1_features VALUES (?)", (recent.isoformat(),))
        done = retention.run(store, now=NOW)
        assert done["retired_model_calls"] == 12
        assert done["ungradable_calls"] == 1
        assert done["retired_liq_inputs"] == 5
        assert done["older_than_45d:rival_model_shadow_forecasts"] == 1
        assert _families(store) == [("knn21_v1", 2), ("pooled_5m_1273de73aa15", 3),
                                    ("regime_v1", 3)]
        assert retention.run(store, now=NOW)["retired_model_calls"] == 0   # repeat is a no-op
    finally:
        store.close()


def test_cleanup_skips_tables_that_do_not_exist(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    try:
        assert retention.run(store, now=NOW) == {}
    finally:
        store.close()


def test_new_path_target_rows_obey_existing_45_day_prediction_retention(tmp_path):
    store = SQLiteHotStore(tmp_path / "path-retention.sqlite")
    try:
        with store.connect() as conn:
            conn.execute(
                "CREATE TABLE rival_forward_path_targets (issued_at TEXT NOT NULL)"
            )
            old = (NOW - timedelta(days=50)).isoformat()
            recent = (NOW - timedelta(days=28)).isoformat()
            conn.execute("INSERT INTO rival_forward_path_targets VALUES (?)", (old,))
            conn.execute("INSERT INTO rival_forward_path_targets VALUES (?)", (recent,))
        report = retention.run(store, now=NOW)
        assert report["older_than_45d:rival_forward_path_targets"] == 1
        with store.connect() as conn:
            values = conn.execute(
                "SELECT issued_at FROM rival_forward_path_targets"
            ).fetchall()
        assert [row["issued_at"] for row in values] == [recent]
    finally:
        store.close()
