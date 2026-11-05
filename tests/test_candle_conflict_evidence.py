"""A conflicting candle keeps the original and logs what differed and when."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

OPENED = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)


def _bar(observed_after_close: int, close: float, trades: int) -> MarketCandle:
    closed = OPENED + timedelta(minutes=5) - timedelta(milliseconds=1)
    return MarketCandle(
        venue="dydx", contract="SOL-USD", quote="USD", interval_minutes=5,
        opened_at=OPENED, closed_at=closed,
        observed_at=closed + timedelta(seconds=observed_after_close),
        open_price=150.0, high_price=151.0, low_price=149.0, close_price=close,
        volume_base=10.0, trades=trades)


def _batch(bar: MarketCandle) -> MarketBatch:
    return MarketBatch((), (bar,), 5, bar.observed_at)


def test_conflict_log_shows_original_and_incoming_values_and_timing(tmp_path, caplog):
    store = SQLiteHotStore(tmp_path / "conflict.sqlite")
    try:
        market = PerpetualMarketStore(store)
        assert market.ingest(_batch(_bar(12, 150.5, 40))).inserted_candles == 1
        revised = _bar(95, 150.7, 44)
        with caplog.at_level(logging.WARNING):
            result = market.ingest(_batch(revised))
        assert result.conflicted_candles == 1
        message = next(r.getMessage() for r in caplog.records
                       if "immutable candle conflicts" in r.getMessage())
        assert "close_price 150.5->150.7" in message
        assert "trades 40->44" in message
        assert "seen +12s (original) vs +95s (incoming) after close" in message
        rows = market.recent_candles(venue="dydx", contract="SOL-USD", quote="USD",
                                     interval_minutes=5, as_of=revised.observed_at)
        assert float(rows[-1]["close_price"]) == 150.5  # original kept
    finally:
        store.close()


def test_identical_replay_is_not_a_conflict(tmp_path):
    store = SQLiteHotStore(tmp_path / "replay.sqlite")
    try:
        market = PerpetualMarketStore(store)
        bar = _bar(12, 150.5, 40)
        market.ingest(_batch(bar))
        again = market.ingest(_batch(replace(bar, observed_at=bar.observed_at + timedelta(minutes=5))))
        assert again.conflicted_candles == 0 and again.unchanged_candles == 1
    finally:
        store.close()
