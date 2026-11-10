"""RIVAL-N: the watchdog's heavy health-check aggregates are cached for 10 minutes.

Mirrors test_backlog_matches_settlement.py: the settlement-backlog join and
the quote-coverage join run on every 2-minute watchdog pass; like the 7-day
regression aggregate, their verdicts barely move in minutes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import (
    REGRESSION_RECHECK,
    RuntimeHealthMonitor,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def _create_cycle_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
        slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
        math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
        math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
        model_review_ok INTEGER NOT NULL, auto_ai_ok INTEGER NOT NULL)""")


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


def _shadow_forecast(conn, issued):
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
             horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
             probability_up,incumbent_up,baseline_up,training_count,training_sha256,
             settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("hyperliquid", "BTC-USD", "USD", "BTC", 5, "flow_v2", issued.isoformat(),
         issued.isoformat(), issued.isoformat(), .9, .5, .5, 10, "0" * 64,
         None, None, None, None, None))


def _quote_row(conn, issued):
    conn.execute(
        """INSERT INTO rival_family_quote_shadow (venue,contract,quote,asset,
             horizon_minutes,family,reference_opened_at,issued_at,target_close_at,
             training_sha256,challenger_up,incumbent_up,challenger_side,incumbent_side,
             entry_at,entry_bid,entry_ask,quantity,state)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("hyperliquid", "BTC-USD", "USD", "BTC", 5, "flow_v2", issued.isoformat(),
         issued.isoformat(), (issued + timedelta(minutes=5)).isoformat(),
         "0" * 64, .9, .5, "UP", "UP", issued.isoformat(), 100.0, 100.1, 1.0, "OPEN"))


def test_backlog_verdict_is_reused_for_ten_minutes_then_rechecked(tmp_path):
    """The settlement-backlog join runs at most every 10 minutes, same alert."""
    store = SQLiteHotStore(tmp_path / "backlog_cache.sqlite")
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        with store.connect() as conn:
            _create_cycle_table(conn)
            target = NOW - timedelta(minutes=40)
            _forecast(conn, "okx", target - timedelta(minutes=1), target)
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        assert monitor.inspect(now=NOW, learning_enabled=True).get(
            "settlement_backlog") == "1 recoverable prospective outcomes"
        with store.connect() as conn:  # the backlog is settled in the ledger...
            conn.execute(
                "UPDATE perp_forward_audit SET settled_at=?", (NOW.isoformat(),))
        # ...but within the recheck window the verdict is reused,
        assert monitor.inspect(now=NOW + timedelta(minutes=2), learning_enabled=True).get(
            "settlement_backlog") == "1 recoverable prospective outcomes"
        # and after it the ledger is read again.
        later = NOW + REGRESSION_RECHECK + timedelta(seconds=1)
        assert "settlement_backlog" not in monitor.inspect(now=later, learning_enabled=True)
    finally:
        store.close()


def test_quote_coverage_verdict_is_reused_for_ten_minutes_then_rechecked(tmp_path):
    """The shadow/quote join runs at most every 10 minutes, same alert."""
    store = SQLiteHotStore(tmp_path / "quote_cache.sqlite")
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        ShadowModelCompetition(store).initialize()
        FamilyQuoteShadow(store).initialize()
        with store.connect() as conn:
            _create_cycle_table(conn)
            for i in range(12):  # all inside the 2-hour window, none matched
                _shadow_forecast(conn, NOW - timedelta(minutes=65 - 5 * i))
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)
        assert "quote_coverage" in monitor.inspect(
            now=NOW, learning_enabled=True, streaming_enabled=True)
        with store.connect() as conn:  # every forecast now has a matched book...
            for i in range(12):
                _quote_row(conn, NOW - timedelta(minutes=65 - 5 * i))
        # ...but within the recheck window the verdict is reused,
        assert "quote_coverage" in monitor.inspect(
            now=NOW + timedelta(minutes=2), learning_enabled=True, streaming_enabled=True)
        # and after it the ledger is read again.
        later = NOW + REGRESSION_RECHECK + timedelta(seconds=1)
        assert "quote_coverage" not in monitor.inspect(
            now=later, learning_enabled=True, streaming_enabled=True)
    finally:
        store.close()
