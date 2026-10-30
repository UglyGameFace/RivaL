"""Every candle passes the same settlement rule, whatever the batch size.

A just-closed single candle used to bypass the 10-second margin entirely:
it could be ingested and then revised by the exchange, leaving the first
(possibly wrong) version stored while per-bar isolation kept it. Now the
candle is held one cycle and qualifies once it is old enough.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.collector import (
    _retain_settled_candles,
)
from parlay_bot.perpetuals.hyperliquid import (
    MarketBatch,
    MarketCandle,
    MarketDataError,
)


def candle(closed, *, venue="hyperliquid"):
    opened = closed - timedelta(minutes=5)
    return MarketCandle(
        venue, "BTC", "USDC", 5, opened, closed,
        closed + timedelta(milliseconds=1),
        80999, 81005, 80991, 81001, 20.0, 42,
    )


def batch(candles, observed):
    return MarketBatch((), tuple(candles), 5, observed)


def test_single_settled_candle_passes_whole():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    kept = _retain_settled_candles(
        batch([candle(now - timedelta(seconds=60))], now)
    )
    assert len(kept.candles) == 1


def test_single_just_closed_candle_is_held_not_ingested():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    with pytest.raises(MarketDataError):
        _retain_settled_candles(
            batch([candle(now - timedelta(seconds=5))], now)
        )


def test_single_unfinished_candle_never_passes():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    with pytest.raises(MarketDataError):
        _retain_settled_candles(
            batch([candle(now + timedelta(seconds=30))], now)
        )


def test_mixed_batch_keeps_only_settled_candles():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    kept = _retain_settled_candles(batch([
        candle(now - timedelta(minutes=10)),
        candle(now - timedelta(seconds=5)),
    ], now))
    assert len(kept.candles) == 1
    assert kept.candles[0].closed_at == now - timedelta(minutes=10)
