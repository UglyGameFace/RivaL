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


# ---------------------------------------------------------------------------
# Version 2: v1 + Binance market-wide context for the coin (buyer/seller
# pressure, trade activity, perp premium over index, last settled funding).
# Built from the same official Binance files the Drive warehouse stores, and
# fetched live from the same Binance endpoints at forecast time.
# ---------------------------------------------------------------------------
FEATURE_VERSION_V2 = "pooled-ohlcv-btc-calendar-binance-context-v2"
CONTEXT_FEATURES = (
    "taker_imb", "taker_imb_12", "trades_ratio", "avg_trade_size",
    "prem", "prem_chg_3", "prem_chg_12", "prem_z", "fund",
)
FEATURES_V2 = FEATURES + CONTEXT_FEATURES
FEATURE_SETS = {FEATURE_VERSION: FEATURES, FEATURE_VERSION_V2: FEATURES_V2}


def funding_known_at(funding_time: datetime) -> datetime:
    """A settled funding rate counts as known one minute after its minute."""
    return funding_time.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _div(a: float, b: float) -> float:
    return a / b if b and math.isfinite(a) and math.isfinite(b) else NAN


def context_vector(bundle: dict | None, close_at: datetime) -> tuple[float, ...]:
    """Context inputs at bar close `close_at` (all NaN when unavailable).

    bundle: {"klines": [(close_at, volume, taker_buy_volume, trades)],
             "premium": {close_at: premium_close}, "funding": [(time, rate)]}
    """
    blank = tuple(NAN for _ in CONTEXT_FEATURES)
    if not bundle:
        return blank
    bars = [b for b in bundle.get("klines", ()) if b[0] <= close_at]
    if len(bars) < WINDOW or bars[-1][0] != close_at:
        return blank
    bars = bars[-WINDOW:]
    if any(b[0] - a[0] != timedelta(minutes=5) for a, b in itertools.pairwise(bars)):
        return blank
    v = [float(b[1]) for b in bars]
    tb = [float(b[2]) for b in bars]
    n = [float(b[3]) for b in bars]
    out: dict[str, float] = {}
    out["taker_imb"] = _div(tb[-1], v[-1]) - 0.5
    out["taker_imb_12"] = _div(sum(tb[-12:]), sum(v[-12:])) - 0.5
    out["trades_ratio"] = math.log((n[-1] + 1) / (sum(n[-288:]) / 288 + 1))
    size = _div(v[-1], n[-1])
    typical = _div(sum(v[-288:]), sum(n[-288:]))
    out["avg_trade_size"] = math.log(size / typical) if size > 0 and typical > 0 else NAN
    premium = bundle.get("premium") or {}
    p = [premium.get(b[0], NAN) for b in bars]
    out["prem"] = p[-1] * 1e4
    out["prem_chg_3"] = (p[-1] - p[-4]) * 1e4
    out["prem_chg_12"] = (p[-1] - p[-13]) * 1e4
    std = _std(p[-288:], 1)
    out["prem_z"] = (p[-1] - sum(p[-288:]) / 288) / std if std and not math.isnan(std) else NAN
    known = [rate for when, rate in bundle.get("funding", ())
             if funding_known_at(when) <= close_at]
    out["fund"] = known[-1] * 1e4 if known else NAN
    return tuple(_f32(out[name]) for name in CONTEXT_FEATURES)


def model_vector(doc: dict, candles, *, asset: str, btc_candles=None,
                 context: dict | None = None) -> tuple[float, ...] | None:
    """The vector a given model document expects (v1 or v2)."""
    base = live_vector(candles, asset=asset, btc_candles=btc_candles)
    if base is None:
        return None
    if doc.get("feature_version") != FEATURE_VERSION_V2:
        return base
    extra = context_vector(context, bar_close(candles[-1]))
    if all(math.isnan(x) for x in extra):
        return None  # a v2 model never predicts without its context inputs
    return base + extra
