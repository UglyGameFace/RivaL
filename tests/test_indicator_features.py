"""Indicator features: causal states, clamped, and consistent with training.

The live vector (_features) must match the training vector (live_examples)
exactly, or the model trains on one distribution and predicts on another.
These tests pin the indicator math and that consistency.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.features import indicator_features, live_examples
from parlay_bot.perpetuals.forward_audit import _features
from parlay_bot.perpetuals.hyperliquid import MarketCandle

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def window(closes, *, volume=10.0):
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [volume] * len(closes)
    return closes, highs, lows, volumes


def test_indicator_states_follow_trend_direction():
    rising = [100.0 + i * 0.5 for i in range(40)]
    rsi, macd, bollinger, atr, _volume = indicator_features(*window(rising))
    assert rsi > 4.0
    # A perfectly linear rise has constant MACD, so its histogram is zero:
    # the histogram measures acceleration, not direction.
    assert macd == pytest.approx(0.0)
    assert bollinger > 0
    assert atr > 0
    accelerating = [100.0 + (i * i) * 0.05 for i in range(40)]
    _, macd, _, _, _ = indicator_features(*window(accelerating))
    assert macd > 0
    falling = [120.0 - i * 0.5 for i in range(40)]
    rsi, macd, bollinger, atr, _volume = indicator_features(*window(falling))
    assert rsi < -4.0
    assert bollinger < 0
    decelerating = [200.0 - (i * i) * 0.05 for i in range(40)]
    _, macd, _, _, _ = indicator_features(*window(decelerating))
    assert macd < 0


def test_indicator_states_are_neutral_on_flat_prices():
    flat = [100.0] * 40
    rsi, macd, bollinger, atr, volume = indicator_features(
        flat, flat, flat, [10.0] * 40)
    assert rsi == pytest.approx(0.0)
    assert macd == pytest.approx(0.0)
    assert bollinger == pytest.approx(0.0)
    assert atr == pytest.approx(0.0)
    assert volume == pytest.approx(0.0)


def test_indicator_features_reject_bad_windows():
    closes, highs, lows, volumes = window([100.0] * 40)
    with pytest.raises(ValueError):
        indicator_features(closes[:39], highs[:39], lows[:39], volumes[:39])
    with pytest.raises(ValueError):
        indicator_features(
            [0.0] * 40, highs, lows, volumes)


def test_live_vector_matches_training_vector_for_last_bar():
    candles = []
    for index in range(120):
        opened = START + timedelta(minutes=5 * index)
        closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
        price = 100 + index * 0.3 + (index % 7) * 0.2
        candles.append(MarketCandle(
            venue="dydx", contract="BTC-USD", quote="USD",
            interval_minutes=5, opened_at=opened, closed_at=closed,
            observed_at=closed + timedelta(seconds=1),
            open_price=price, high_price=price + 1,
            low_price=price - 1, close_price=price,
            volume_base=10 + (index % 6), trades=20,
        ))
    samples = live_examples(candles, horizon_minutes=5)
    assert samples
    last = samples[-1]
    assert len(last.features) == 10
    # The last sample's current bar is candles[118] (one horizon bar must
    # follow it), so the live vector over candles[:119] must match exactly.
    assert _features(candles[:119]) == last.features
    with pytest.raises(ValueError):
        _features(candles[:39])
