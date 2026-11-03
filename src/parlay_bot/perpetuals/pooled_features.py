"""Inputs of RivaL's pooled multi-year model, computed live from 5m candles.

The SAME definitions are implemented with pandas for training in
parlay_bot.research.pooled_training; tests/test_pooled_model.py forces both
to produce identical numbers. Every input uses only bars that closed at or
before the issuing bar's close. Missing values are NaN (the trees route them).
"""
from __future__ import annotations

import itertools
import math
import struct
from datetime import UTC, datetime, timedelta

FEATURE_VERSION = "pooled-ohlcv-btc-calendar-v1"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
FEATURES = (
    "ret_1", "ret_12", "vol_12", "range", "vol_ratio", "rsi", "macd_hist",
    "boll_z", "atr", "vol_regime", "ret_3", "ret_48", "ret_288", "vol_288",
    "vol_short_long", "to_funding", "hour_sin", "hour_cos", "dow",
    "btc_ret_1", "btc_ret_12", "rel_ret_12", "coin",
)
# Bars needed so exponential averages have converged to the training values
# (weights older than this are below 1e-11) and the 288-bar window is full.
WINDOW = 420
NAN = float("nan")


def _f32(value: float) -> float:
    """Training casts inputs to float32; match it exactly before tree splits."""
    if not math.isfinite(value):
        return NAN
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _ewm(values: list[float], alpha: float) -> list[float]:
    """pandas .ewm(alpha=..., adjust=False).mean() with leading NaNs skipped."""
    out, current = [], NAN
    for value in values:
        if math.isnan(value):
            out.append(current)
            continue
        current = value if math.isnan(current) else alpha * value + (1 - alpha) * current
        out.append(current)
    return out


def _std(values: list[float], ddof: int) -> float:
    n = len(values)
    if n - ddof <= 0 or any(math.isnan(v) for v in values):
        return NAN
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - ddof))


def _log(a: float, b: float) -> float:
    return math.log(a / b) if a > 0 and b > 0 else NAN


def bar_close(candle) -> datetime:
    opened = candle.opened_at if isinstance(candle.opened_at, datetime) else \
        datetime.fromisoformat(str(candle.opened_at))
    opened = opened.astimezone(UTC) if opened.utcoffset() else opened.replace(tzinfo=UTC)
    return opened + timedelta(minutes=5)


def contiguous(candles) -> bool:
    return all(
        (bar_close(b) - bar_close(a)) == timedelta(minutes=5)
        for a, b in itertools.pairwise(candles)
    )


def live_vector(candles, *, asset: str, btc_candles=None) -> tuple[float, ...] | None:
    """Feature vector at the last candle's close, or None without enough history."""
    if asset not in ASSETS or len(candles) < WINDOW:
        return None
    window = list(candles[-WINDOW:])
    if not contiguous(window):
        return None
    c = [float(x.close_price) for x in window]
    h = [float(x.high_price) for x in window]
    lo = [float(x.low_price) for x in window]
    v = [float(x.volume_base) for x in window]
    if any(not math.isfinite(x) or x <= 0 for x in c):
        return None
    lr = [NAN] + [_log(c[i], c[i - 1]) for i in range(1, len(c))]
    last = len(c) - 1
    out: dict[str, float] = {}
    out["ret_1"] = lr[last] * 100
    out["ret_12"] = _log(c[last], c[last - 12]) * 100
    vol_12 = _std(lr[last - 11:], 1) * 100
    out["vol_12"] = vol_12
    out["range"] = (h[last] - lo[last]) / c[last] * 100
    out["vol_ratio"] = math.log((v[last] + 1) / (sum(v[last - 12:last]) / 12 + 1))
    diff = [NAN] + [c[i] - c[i - 1] for i in range(1, len(c))]
    gain = _ewm([NAN if math.isnan(d) else max(d, 0.0) for d in diff], 1 / 14)
    loss = _ewm([NAN if math.isnan(d) else max(-d, 0.0) for d in diff], 1 / 14)
    out["rsi"] = (100 - 100 / (1 + gain[last] / loss[last]) - 50) / 10 \
        if loss[last] and not math.isnan(loss[last]) else NAN
    ema12, ema26 = _ewm(c, 2 / 13), _ewm(c, 2 / 27)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = _ewm(macd, 2 / 10)
    out["macd_hist"] = (macd[last] - signal[last]) / c[last] * 100
    std20 = _std(c[last - 19:], 0)
    mean20 = sum(c[last - 19:]) / 20
    out["boll_z"] = (c[last] - mean20) / (2 * std20) if std20 else NAN
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
          for i in range(last - 13, last + 1)]
    out["atr"] = sum(tr) / 14 / c[last] * 100
    out["vol_regime"] = math.log((sum(v[last - 4:]) / 5 + 1) / (sum(v[last - 39:]) / 40 + 1))
    out["ret_3"] = _log(c[last], c[last - 3]) * 100
    out["ret_48"] = _log(c[last], c[last - 48]) * 100
    out["ret_288"] = _log(c[last], c[last - 288]) * 100
    vol_288 = _std(lr[last - 287:], 1) * 100
    out["vol_288"] = vol_288
    out["vol_short_long"] = math.log((vol_12 + 1e-6) / (vol_288 + 1e-6)) \
        if not (math.isnan(vol_12) or math.isnan(vol_288)) else NAN
    t = bar_close(window[-1])
    hours = t.hour + t.minute / 60
    out["to_funding"] = (8 - (hours % 8)) / 8
    out["hour_sin"] = math.sin(2 * math.pi * hours / 24)
    out["hour_cos"] = math.cos(2 * math.pi * hours / 24)
    out["dow"] = float(t.weekday())
    btc = {bar_close(x): float(x.close_price) for x in (btc_candles or ())[-20:]}
    b0, b1, b12 = btc.get(t), btc.get(t - timedelta(minutes=5)), btc.get(t - timedelta(minutes=60))
    if asset == "BTC" or not btc_candles:
        out["btc_ret_1"] = out["btc_ret_12"] = out["rel_ret_12"] = NAN
    else:
        out["btc_ret_1"] = _log(b0, b1) * 100 if b0 and b1 else NAN
        out["btc_ret_12"] = _log(b0, b12) * 100 if b0 and b12 else NAN
        out["rel_ret_12"] = (out["ret_12"] - out["btc_ret_12"]
                             if not math.isnan(out["btc_ret_12"]) else NAN)
    out["coin"] = float(ASSETS.index(asset))
    return tuple(_f32(out[name]) for name in FEATURES)
