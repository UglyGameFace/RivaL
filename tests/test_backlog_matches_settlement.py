"""The backlog alert counts only outcomes settlement will actually accept."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def _forecast(conn, venue, issued, target):
    conn.execute(
        """INSERT INTO perp_forward_audit (venue,contract,quote,asset,horizon_minutes,
           model_version,reference_opened_at,reference_close,issued_at,target_opened_at,
           probability_up,baseline_up,training_evaluated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (venue, "BTC-X", "USDT", "BTC", 5, MODEL, (target - timedelta(minutes=5)).isoformat(),
         100.0, issued.isoformat(), target.isoformat(), 0.5, 0.5, 30))
    conn.execute(
        """INSERT INTO perp_market_candles (venue,contract,quote,interval_minutes,opened_at,
           closed_at,first_observed_at,open_price,high_price,low_price,close_price,
           volume_base,trades) VALUES (?,?,?,5,?,?,?,100,101,99,100.5,1,3)""",
        (venue, "BTC-X", "USDT", target.isoformat(), (target + timedelta(minutes=5)).isoformat(),
         (target + timedelta(minutes=5, seconds=30)).isoformat()))


def test_rows_settlement_rejects_are_not_called_recoverable(tmp_path):
    store = SQLiteHotStore(tmp_path / "backlog.sqlite")
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        with store.connect() as conn:  # the learning heartbeat table, empty
            conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
                math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
                math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
                model_review_ok INTEGER NOT NULL, gemini_ok INTEGER NOT NULL)""")
        target = NOW - timedelta(minutes=40)
        with store.connect() as conn:
            _forecast(conn, "okx", target - timedelta(minutes=1), target)  # recoverable
            # Issued AFTER its target candle closed: settlement skips it forever.
            _forecast(conn, "gate", target + timedelta(minutes=5, seconds=10), target)
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        faults = monitor.inspect(now=NOW, learning_enabled=True)
        assert faults.get("settlement_backlog") == "1 recoverable prospective outcomes"
    finally:
        store.close()


def test_regression_verdict_is_reused_for_ten_minutes_then_rechecked(tmp_path):
    """The 7-day challenger aggregate runs at most every 10 minutes, same alerts."""
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.perpetuals.runtime_health import REGRESSION_RECHECK

    store = SQLiteHotStore(tmp_path / "regression_cache.sqlite")
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        ShadowModelCompetition(store).initialize()
        with store.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
                math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
                math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
                model_review_ok INTEGER NOT NULL, gemini_ok INTEGER NOT NULL)""")
            for i in range(60):
                at = (NOW - timedelta(minutes=300 - 5 * i)).isoformat()
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                         horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                         probability_up,incumbent_up,baseline_up,training_count,training_sha256,
                         settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("dydx", "BTC-USD", "USD", "BTC", 5, "flow_v2", at, at, at,
                     .9, .5, .5, 10, "0" * 64, at, 0, .81, .25, .25))
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        key = "regression:dydx:BTC-USD:flow_v2"
        assert key in monitor.inspect(now=NOW, learning_enabled=True)
        with store.connect() as conn:  # the regression is fixed in the ledger...
            conn.execute("UPDATE rival_model_shadow_forecasts SET model_brier=.2")
        # ...but within the recheck window the verdict is reused,
        assert key in monitor.inspect(now=NOW + timedelta(minutes=2), learning_enabled=True)
        # and after it the ledger is read again.
        later = NOW + REGRESSION_RECHECK + timedelta(seconds=1)
        assert key not in monitor.inspect(now=later, learning_enabled=True)
    finally:
        store.close()


def test_a_retired_challenger_keeps_its_record_but_raises_no_incident(tmp_path):
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition

    store = SQLiteHotStore(tmp_path / "retired.sqlite")
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        ShadowModelCompetition(store).initialize()
        with store.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
                math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
                math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
                model_review_ok INTEGER NOT NULL, gemini_ok INTEGER NOT NULL)""")
            for i in range(60):  # losing badly, but last issued 3 hours ago
                at = (NOW - timedelta(hours=8, minutes=-5 * i)).isoformat()
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                         horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                         probability_up,incumbent_up,baseline_up,training_count,training_sha256,
                         settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("dydx", "BTC-USD", "USD", "BTC", 5, "flow_v1", at, at, at,
                     .9, .5, .5, 10, "0" * 64, at, 0, .81, .25, .25))
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        faults = monitor.inspect(now=NOW, learning_enabled=True)
        assert not any(key.startswith("regression:") for key in faults)
    finally:
        store.close()


def test_an_outcome_that_just_arrived_is_not_a_backlog_until_two_cycles_pass(tmp_path):
    from parlay_bot.perpetuals.runtime_health import BACKLOG_GRACE

    store = SQLiteHotStore(tmp_path / "grace.sqlite")
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        with store.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
                math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
                math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
                model_review_ok INTEGER NOT NULL, gemini_ok INTEGER NOT NULL)""")
            target = NOW - timedelta(minutes=40)
            _forecast(conn, "okx", target - timedelta(minutes=1), target)
            # The outcome candle reached RivaL late: 2 minutes ago.
            conn.execute("UPDATE perp_market_candles SET first_observed_at=?",
                         ((NOW - timedelta(minutes=2)).isoformat(),))
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        assert "settlement_backlog" not in monitor.inspect(now=NOW, learning_enabled=True)
        later = NOW + BACKLOG_GRACE
        assert monitor.inspect(now=later, learning_enabled=True).get(
            "settlement_backlog") == "1 recoverable prospective outcomes"
    finally:
        store.close()
