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
