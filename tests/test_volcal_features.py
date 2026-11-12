"""volcal_v1 feature contract: frame/live twins, DC-leg confirmation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.research import archive_features, volcal_features
from parlay_bot.research.volcal_features import (
    FEATURE_DIGEST,
    FEATURES,
    FLAG_NAMES,
    GROUPS,
    dc_leg_series,
    frame_new_features,
    live_vector,
)

START = datetime(2026, 10, 1, tzinfo=UTC)


def _candles(closes, volumes=None, highs=None, lows=None):
    out = []
    n = len(closes)
    volumes = volumes if volumes is not None else [10.0] * n
    highs = highs if highs is not None else [c * 1.002 for c in closes]
    lows = lows if lows is not None else [c * 0.998 for c in closes]
    for i, close in enumerate(closes):
        opened = START + timedelta(minutes=5 * i)
        closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
        out.append(MarketCandle(
            venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
            interval_minutes=5, opened_at=opened, closed_at=closed,
            observed_at=closed + timedelta(seconds=1),
            open_price=closes[i - 1] if i else close,
            high_price=highs[i], low_price=lows[i], close_price=close,
            volume_base=float(volumes[i]), trades=3,
        ))
    return out


def _frame_from(candles):
    import pandas as pd

    rows = [{
        "open": c.open_price, "high": c.high_price, "low": c.low_price,
        "close": c.close_price, "volume": c.volume_base,
    } for c in candles]
    index = pd.DatetimeIndex([c.closed_at for c in candles])
    k = pd.DataFrame(rows, index=index)
    base = archive_features.frame_features(k, None, "BTC")
    new = frame_new_features(k)
    joined = pd.concat([base[list(volcal_features.BASE_FEATURES)], new], axis=1)
    return joined[list(FEATURES)]


def _random_walk(n=600, seed=7, drift=0.0002, sigma=0.002):
    rng = np.random.default_rng(seed)
    logrets = rng.normal(drift, sigma, size=n)
    closes = 100.0 * np.exp(np.cumsum(logrets))
    return closes.tolist()


def test_feature_contract_shape():
    assert len(FEATURES) == 22
    assert FEATURES[:10] == volcal_features.BASE_FEATURES
    assert FEATURES[10:] == volcal_features.NEW_FEATURES
    assert len(set(FEATURES)) == 22
    assert FLAG_NAMES == ("candle", "vol", "compression", "volume", "dc")
    # Groups partition the 22 indices exactly.
    covered = sorted(i for group in GROUPS.values() for i in group)
    assert covered == list(range(22))
    import hashlib

    assert FEATURE_DIGEST == hashlib.sha256(",".join(FEATURES).encode()).hexdigest()


def test_frame_and_live_agree():
    closes = _random_walk()
    candles = _candles(closes)
    frame = _frame_from(candles)
    # The archive frame blanks rows whose 420-bar window is not full, so
    # the joined frame is only complete from bar 420; live_vector needs
    # 300 bars on its own.
    for end in (420, 500, 600):
        values, flags = live_vector(candles[:end], asset="BTC")
        assert len(values) == 22
        assert flags == (1.0,) * 5
        row = frame.iloc[end - 1].to_numpy(dtype=float)
        assert np.allclose(values, row, rtol=1e-6, atol=1e-9, equal_nan=True), end
        assert np.all(np.isfinite(values))


def test_live_vector_requires_window_and_asset():
    closes = _random_walk(n=299)
    assert live_vector(_candles(closes), asset="BTC") is None
    closes = _random_walk(n=300)
    assert live_vector(_candles(closes), asset="DOGE") is None
    assert live_vector(_candles(closes), asset="BTC") is not None


def test_vol_ratios_track_recent_volatility():
    # 288 quiet bars, then 12 loud bars: fast/base ratios must exceed 1.
    rng = np.random.default_rng(3)
    quiet = rng.normal(0.0, 0.0005, size=288)
    loud = rng.normal(0.0, 0.005, size=72)
    closes = 100.0 * np.exp(np.cumsum(np.concatenate([quiet, loud])))
    candles = _candles(closes.tolist())
    values, _ = live_vector(candles, asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["vol_ratio_1h_24h"] > 1.0
    assert by_name["vol_ratio_6h_24h"] > 1.0
    assert 0.0 < by_name["vol_24h"] <= 5.0
    # A uniformly volatile stretch has ratios near 1.
    closes = _random_walk(n=400, sigma=0.002)
    values, _ = live_vector(_candles(closes), asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert 0.5 < by_name["vol_ratio_1h_24h"] < 2.0


def test_compression_compares_8_vs_96_bars():
    # Volatile 96 bars (wide high-low spreads, moving closes), then 8
    # dead-flat bars (no spread, no move): compression below 1.
    rng = np.random.default_rng(5)
    volatile = rng.normal(0.0, 0.004, size=352)
    flat = np.zeros(8)
    closes = 100.0 * np.exp(np.cumsum(np.concatenate([volatile, flat])))
    closes = closes.tolist()
    highs = [c * 1.01 for c in closes[:352]] + closes[352:]
    lows = [c * 0.99 for c in closes[:352]] + closes[352:]
    candles = _candles(closes, highs=highs, lows=lows)
    values, _ = live_vector(candles, asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["compr_range"] < 1.0
    assert by_name["compr_ret"] < 1.0
    # The reverse (flat history, loud last 8) reads above 1.
    loud = rng.normal(0.0, 0.006, size=8)
    closes = 100.0 * np.exp(np.cumsum(np.concatenate([np.zeros(352), loud])))
    closes = closes.tolist()
    highs = closes[:352] + [c * 1.01 for c in closes[352:]]
    lows = closes[:352] + [c * 0.99 for c in closes[352:]]
    values, _ = live_vector(_candles(closes, highs=highs, lows=lows), asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["compr_range"] > 1.0
    assert by_name["compr_ret"] > 1.0


def test_volz_spike_and_collapse():
    closes = _random_walk(n=400)
    volumes = [10.0] * 400
    volumes[-1] = 100.0  # 10x the trailing median -> log(10), clipped to 2
    values, _ = live_vector(_candles(closes, volumes=volumes), asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["volz"] == pytest.approx(2.0)
    assert by_name["vol_zscore"] > 0
    volumes[-1] = 1.0  # collapse below the median
    values, _ = live_vector(_candles(closes, volumes=volumes), asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["volz"] < 0
    assert by_name["vol_zscore"] < 0


def test_volz_zero_volume_last_bar_matches_frame():
    # A last bar with no trades over a nonzero median: log(0) is -inf in
    # the frame (clipped to -2); the live twin must not raise.
    closes = _random_walk(n=500)
    volumes = [10.0] * 500
    volumes[-1] = 0.0
    candles = _candles(closes, volumes=volumes)
    values, _ = live_vector(candles, asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["volz"] == pytest.approx(-2.0)
    assert np.all(np.isfinite(values))
    frame = _frame_from(candles)
    assert frame["volz"].iloc[-1] == pytest.approx(by_name["volz"])


def test_dc_leg_confirms_on_close_retrace():
    rng = np.random.default_rng(11)
    # A steady rise with small noise, then one sharp closing drop.
    drift = rng.normal(0.002, 0.001, size=120)
    closes = 100.0 * np.exp(np.cumsum(drift))
    closes = closes.tolist()
    closes.append(closes[-1] * 0.97)  # -3% close: many trailing sigmas
    age, dist, conf = dc_leg_series(closes)
    assert conf[-2] == 0.0  # still rising: the leg is unconfirmed
    assert conf[-1] == 1.0
    assert dist[-1] >= 1.0
    assert age[-1] > 0.0
    # The new leg starts at the extreme: the next bar is young again.
    closes.append(closes[-1] * 0.999)
    age, dist, conf = dc_leg_series(closes)
    assert conf[-1] == 0.0
    assert age[-1] < age[-2]


def test_dc_leg_wick_does_not_confirm():
    # A big high wick with the close still at the extreme: no confirmation.
    rng = np.random.default_rng(11)
    drift = rng.normal(0.002, 0.001, size=400)
    closes = (100.0 * np.exp(np.cumsum(drift))).tolist()
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    highs[-1] = closes[-1] * 1.05  # 5% wick, close unchanged
    lows[-1] = closes[-1] * 0.95
    _age, dist, conf = dc_leg_series(closes)
    assert conf[-1] == 0.0
    assert dist[-1] == 0.0
    values, _ = live_vector(_candles(closes, highs=highs, lows=lows), asset="BTC")
    by_name = dict(zip(FEATURES, values))
    assert by_name["dc_confirmed"] == 0.0


def test_flat_series_is_finite_and_neutral():
    closes = [100.0] * 400
    values, flags = live_vector(_candles(closes), asset="BTC")
    assert values is not None
    assert np.all(np.isfinite(values))
    by_name = dict(zip(FEATURES, values))
    # No volatility change, no volume surprise, no leg movement.
    assert by_name["vol_ratio_1h_24h"] == pytest.approx(1.0)
    assert by_name["vol_ratio_6h_24h"] == pytest.approx(1.0)
    assert by_name["compr_range"] == pytest.approx(1.0)
    assert by_name["compr_ret"] == pytest.approx(1.0)
    assert by_name["volz"] == pytest.approx(0.0)
    assert by_name["vol_zscore"] == pytest.approx(0.0)
    assert by_name["dc_confirmed"] == 0.0
    assert by_name["dc_dist"] == 0.0
    assert flags == (1.0,) * 5


def test_frame_new_features_window():
    closes = _random_walk(n=400)
    candles = _candles(closes)
    import pandas as pd

    rows = [{"open": c.open_price, "high": c.high_price, "low": c.low_price,
             "close": c.close_price, "volume": c.volume_base} for c in candles]
    k = pd.DataFrame(rows, index=pd.DatetimeIndex([c.closed_at for c in candles]))
    new = frame_new_features(k)
    assert list(new.columns) == list(volcal_features.NEW_FEATURES)
    # Rows before the 288-bar window are NaN; the last row is complete.
    assert new.iloc[:287].isna().any(axis=1).all()
    assert np.all(np.isfinite(new.iloc[-1].to_numpy(dtype=float)))
