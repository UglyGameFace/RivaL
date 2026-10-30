"""Flat outcomes settle as NOT UP, and stale forecasts reconcile.

A target that closes exactly at the reference price used to stay pending
forever (the settlement query excluded ties, and the training extractor
dropped them). UP is a strict price increase, so a tie settles as NOT UP
in both places, keeping train and serve consistent. Separately, forecasts
whose outcome arrives more than 48 hours after the target used to fall
out of the settlement scan entirely; a bounded hourly reconciliation
revisits them under the same prospective guards.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import live_examples
from parlay_bot.perpetuals.forward_audit import (
    MODEL as FORWARD_MODEL,
)
from parlay_bot.perpetuals.forward_audit import (
    ForwardAccuracyMonitor,
)
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

START = datetime(2026, 10, 20, tzinfo=UTC)


def insert_candle(conn, *, venue="hyperliquid", opened, close, observed):
    closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
    conn.execute(
        """INSERT INTO perp_market_candles
           (venue,contract,quote,interval_minutes,opened_at,closed_at,
            first_observed_at,open_price,high_price,low_price,close_price,
            volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            venue, "BTC", "USDC", 5, opened.isoformat(), closed.isoformat(),
            observed.isoformat(), close, close + 1, close - 1, close, 10, 20,
        ),
    )


def insert_forecast(conn, *, target, issued, reference_close, probability=.7):
    conn.execute(
        """INSERT INTO perp_forward_audit
           (venue,contract,quote,asset,horizon_minutes,model_version,
            reference_opened_at,reference_close,issued_at,target_opened_at,
            probability_up,baseline_up,training_evaluated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
            (target - timedelta(minutes=5)).isoformat(), reference_close,
            issued.isoformat(), target.isoformat(), probability, .5, 50,
        ),
    )


def settled_row(store):
    with store.connect() as conn:
        return conn.execute(
            "SELECT settled_at,outcome_up FROM perp_forward_audit"
        ).fetchone()


def test_flat_close_settles_as_not_up(tmp_path):
    now = START + timedelta(hours=2)
    store = SQLiteHotStore(tmp_path / "tie.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    target = START + timedelta(minutes=60)
    try:
        market.initialize()
        monitor.initialize()
        with store.connect() as conn:
            insert_candle(
                conn, opened=target, close=100.0,
                observed=target + timedelta(minutes=6),
            )
            insert_forecast(
                conn, target=target, issued=target - timedelta(minutes=30),
                reference_close=100.0,
            )
        assert monitor._settle(now) == 1
        row = settled_row(store)
        assert row["settled_at"] is not None
        assert int(row["outcome_up"]) == 0
    finally:
        store.close()


def test_stale_forecast_reconciles_after_two_day_window(tmp_path):
    now = START + timedelta(days=4)
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    target = START + timedelta(minutes=60)
    try:
        market.initialize()
        monitor.initialize()
        with store.connect() as conn:
            insert_candle(
                conn, opened=target, close=101.0,
                observed=target + timedelta(minutes=6),
            )
            insert_forecast(
                conn, target=target, issued=target - timedelta(minutes=30),
                reference_close=100.0,
            )
        assert monitor._settle(now) == 0
        assert monitor.reconcile_stale_settlements(now) == 1
        row = settled_row(store)
        assert row["settled_at"] is not None
        assert int(row["outcome_up"]) == 1
    finally:
        store.close()


def test_reconciliation_keeps_prospective_guard(tmp_path):
    """An outcome observed before issuance never settles, however old."""
    now = START + timedelta(days=4)
    store = SQLiteHotStore(tmp_path / "guard.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    target = START + timedelta(minutes=60)
    try:
        market.initialize()
        monitor.initialize()
        with store.connect() as conn:
            insert_candle(
                conn, opened=target, close=101.0,
                observed=target - timedelta(minutes=40),
            )
            insert_forecast(
                conn, target=target, issued=target - timedelta(minutes=30),
                reference_close=100.0,
            )
        assert monitor.reconcile_stale_settlements(now) == 0
        assert settled_row(store)["settled_at"] is None
    finally:
        store.close()


def test_training_labels_flat_target_as_not_up():
    candles = []
    for index in range(120):
        opened = START + timedelta(minutes=5 * index)
        closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
        price = 100 + index * 0.3
        candles.append(
            MarketCandle(
                venue="dydx", contract="BTC-USD", quote="USD",
                interval_minutes=5, opened_at=opened, closed_at=closed,
                observed_at=closed + timedelta(seconds=1),
                open_price=price, high_price=price + 1,
                low_price=price - 1, close_price=price,
                volume_base=10, trades=20,
            )
        )
    candles[119] = MarketCandle(
        venue="dydx", contract="BTC-USD", quote="USD",
        interval_minutes=5,
        opened_at=candles[119].opened_at, closed_at=candles[119].closed_at,
        observed_at=candles[119].observed_at,
        open_price=candles[118].close_price,
        high_price=candles[118].close_price + 1,
        low_price=candles[118].close_price - 1,
        close_price=candles[118].close_price,
        volume_base=10, trades=20,
    )
    samples = live_examples(candles, horizon_minutes=5)
    assert samples
    last = samples[-1]
    assert last.issued_at == candles[118].observed_at
    assert last.label_up == 0
