"""VolCal (volcal_v1) feature contract.

RivaL-native rebuild of the useful ideas in Obi-Stoned Kenobi's perp
engine (permission: his Discord DM to Felix, 2026-11-11 21:41 EST —
"[...] use the engines, codes, data sets, and everything else I've
sent you for your bots/websites"). Nothing is copied from their code
and none of their fitted weights is imported: every feature below is
computed from RivaL's own candles, and every weight is fitted offline
on RivaL's own archive data.

Contract: the incumbent's 10 candle features, unchanged, plus 12 new
ones in four groups — adaptive volatility, compression, volume
surprises, and directional-change (DC) leg confirmation:

* vol (10-13): trailing std of log-returns on 288/72/12 bars (24h/6h/1h
  on 5-minute bars), the fast/base ratios, and ret_1 divided by current
  volatility.
* compression (14-15): mean per-bar log-range (log(high/low)) over
  the last 8 bars vs the last 96 bars, and mean |log-return| last 8
  vs last 96 bars. (The window-total range would make the ratio
  <= 1 by construction — the 96-bar window contains the 8-bar one —
  so the per-bar mean is the honest comparison.)
* volume (16-18): log(current 15-min volume / trailing-96-bar median
  volume) — their "volz" — plus a volume z-score vs the trailing
  96-bar mean/sd, and mean volume last 12 bars vs last 96 bars (the
  shorter-horizon analogue of their 30-day volume tercile, which is
  not computable live from the 1000-bar read).
* dc (19-21): directional-change leg features. A leg is only recognised
  as ended once price retraces a volatility-scaled threshold theta from
  the leg extreme on a CLOSING bar (single wicks confirm nothing);
  theta is 1.0 sigma of the trailing 288-bar log-return std. Features:
  leg age (log1p bars), distance from the extreme in sigma units, and
  a confirmed flag (1.0 on the bar the retrace first reaches theta).
  No second-candle or retest rule: none was verified in their code.

Admissibility: every feature is computable identically from the
archive frame (``frame_new_features``) and from the live candle read
(``live_vector``); the two twins are forced identical by
tests/test_volcal_features.py. All lookbacks are <= 288 bars, so they
fit the live 1000-bar read and the archive window. WINDOW is 300 bars.

Defects deliberately NOT adopted from their engine: no martingale /
loss-doubling, no probability-as-price P&L, no Down = 1 - Up BUY as an
executable price, no 30-row promotion rule, and 5-minute gets its own
training and evaluation — 15-minute confidence is never borrowed for
5-minute (their wave-key/cascade paths do exactly that).
"""
from __future__ import annotations

import hashlib
import math

FEATURE_CONTRACT = "volcal-candle-vol-v1"
MODEL_VERSION = "volcal-offline-v1"
SCHEMA = "rival-volcal-model-v1"
WINDOW = 300

BASE_FEATURES = (
    "ret_1",
    "ret_5",
    "vol_5",
    "range",
    "vol_regime_5",
    "rsi",
    "macd_hist",
    "boll_z",
    "atr",
    "vol_regime",
)
NEW_FEATURES = (
    "vol_24h",
    "vol_ratio_6h_24h",
    "ret_1_volnorm",
    "vol_ratio_1h_24h",
    "compr_range",
    "compr_ret",
    "volz",
    "vol_zscore",
    "vol_trend_12_96",
    "dc_age",
    "dc_dist",
    "dc_confirmed",
)
FEATURES = BASE_FEATURES + NEW_FEATURES
FEATURE_DIGEST = hashlib.sha256(",".join(FEATURES).encode()).hexdigest()
# Every volcal feature is computed from the venue's own candles — no
# cross-asset, premium, funding, or tape inputs exist in this contract.
FEATURE_SOURCES = {name: ("candles",) for name in FEATURES}
GROUPS = {
    "candle": tuple(range(10)),
    "vol": tuple(range(10, 14)),
    "compression": tuple(range(14, 16)),
    "volume": tuple(range(16, 19)),
    "dc": tuple(range(19, 22)),
}
FLAG_NAMES = ("candle", "vol", "compression", "volume", "dc")

# DC-leg confirmation threshold, in trailing-volatility sigma units.
DC_THETA_SIGMA = 1.0
# Trailing window (bars) for the DC sigma and the vol_24h feature.
# 288 bars = 24h on 5-minute bars, 72h on 15-minute bars, 288h on
# 60-minute bars; the window is counted in bars, not wall-clock time.
DC_SIGMA_BARS = 288


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def _pop_std(values) -> float:
    """Population std (ddof=0), matching features.py's rolling std."""
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(max(var, 0.0))


def _safe_ratio(num: float, den: float, *, neutral: float = 1.0, hi: float = 5.0) -> float:
    """Clamped num/den with the flat-market limit spelled out.

    Both zero -> neutral (no change); num > 0 over a zero denominator
    -> the clamp ceiling. The frame twin uses the same rule, so the
    two agree even on a flat stretch.
    """
    if den == 0.0:
        return neutral if num == 0.0 else hi
    return _clamp(num / den, 0.0, hi)


def _median(values) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def dc_leg_series(closes):
    """Directional-change leg features for every bar.

    Single pass over closing prices (oldest -> newest), O(n). Returns
    three lists aligned with ``closes``: leg age in log1p bars,
    retrace distance from the leg extreme in sigma units, and the
    confirmed flag. Sigma is the trailing 288-bar log-return std,
    computed causally (bars <= the current one only).

    On the bar where the retrace first reaches theta, the features
    describe the leg that just ended (age counted to this bar, distance
    >= 1.0, confirmed = 1.0); the new leg, which starts at the extreme,
    is described from the next bar. A leg that never retraces theta
    stays unconfirmed however old it gets.
    """
    n = len(closes)
    age = [0.0] * n
    dist = [0.0] * n
    confirmed = [0.0] * n
    if n == 0:
        return age, dist, confirmed
    # Running trailing-288 log-return std (population, ddof=0).
    from collections import deque

    window: deque[float] = deque()
    s = 0.0
    ss = 0.0
    direction = 0  # +1 up leg, -1 down leg, 0 no leg yet
    leg_start = 0
    extreme = closes[0]
    extreme_idx = 0
    for i in range(n):
        if i > 0:
            prev = closes[i - 1]
            cur = closes[i]
            lr = math.log(cur / prev) if prev > 0 and cur > 0 else 0.0
            window.append(lr)
            s += lr
            ss += lr * lr
            if len(window) > DC_SIGMA_BARS:
                old = window.popleft()
                s -= old
                ss -= old * old
        m = len(window)
        if m > 1:
            mean = s / m
            var = ss / m - mean * mean
            sigma = math.sqrt(max(var, 0.0))
        else:
            sigma = 0.0
        if i == 0:
            continue
        if direction == 0:
            if closes[i] > closes[i - 1]:
                direction = 1
            elif closes[i] < closes[i - 1]:
                direction = -1
            leg_start = i - 1
            extreme = closes[i]
            extreme_idx = i
            continue
        if ((direction == 1 and closes[i] > extreme)
                or (direction == -1 and closes[i] < extreme)):
            extreme = closes[i]
            extreme_idx = i
        if sigma > 1e-12 and extreme > 0 and closes[i] > 0:
            retrace = abs(math.log(closes[i] / extreme)) / sigma
        else:
            retrace = 0.0
        if retrace >= DC_THETA_SIGMA:
            # The leg is confirmed ended on this closing bar. Emit the
            # ended leg's features, then start the new leg at the extreme.
            confirmed[i] = 1.0
            dist[i] = _clamp(retrace, 0.0, 5.0)
            age[i] = _clamp(math.log1p(i - leg_start), 0.0, 5.0)
            direction = -direction
            leg_start = extreme_idx
            extreme = closes[i]
            extreme_idx = i
        else:
            confirmed[i] = 0.0
            dist[i] = _clamp(retrace, 0.0, 5.0)
            age[i] = _clamp(math.log1p(i - leg_start), 0.0, 5.0)
    return age, dist, confirmed


def frame_new_features(k):
    """pandas twin of the 12 new features over a whole history.

    k: DataFrame indexed by bar CLOSE time with close/high/low/volume.
    Returns a DataFrame with exactly the NEW_FEATURES columns, one row
    per bar. Rows whose 288-bar window is not full are NaN (the caller
    drops them, as the archive twin drops its own short-window rows).
    Flat-market limits follow the same rules as the live twin.
    """
    import numpy as np
    import pandas as pd

    c, h, lo, v = k["close"], k["high"], k["low"], k["volume"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=k.index)
    vol288 = lr.rolling(DC_SIGMA_BARS).std(ddof=0)
    vol72 = lr.rolling(72).std(ddof=0)
    vol12 = lr.rolling(12).std(ddof=0)
    f["vol_24h"] = (vol288 * 100).clip(0, 5)
    f["vol_ratio_6h_24h"] = _ratio_frame(vol72, vol288)
    ret1_raw = lr * 100
    f["ret_1_volnorm"] = (ret1_raw / (vol288 * 100).clip(lower=1e-6)).clip(-5, 5)
    f["vol_ratio_1h_24h"] = _ratio_frame(vol12, vol288)
    per_bar_range = np.log(h / lo)
    f["compr_range"] = _ratio_frame(
        per_bar_range.rolling(8).mean(), per_bar_range.rolling(96).mean())
    meanabs8 = lr.abs().rolling(8).mean()
    meanabs96 = lr.abs().rolling(96).mean()
    f["compr_ret"] = _ratio_frame(meanabs8, meanabs96)
    med96 = v.rolling(96).median()
    volz = np.log(v / med96)
    both_zero = (v == 0) & (med96 == 0)
    volz = volz.where(~both_zero, 0.0)
    f["volz"] = volz.clip(-2, 2)
    mean96 = v.rolling(96).mean()
    std96 = v.rolling(96).std(ddof=0)
    zscore = (v - mean96) / std96
    zscore = zscore.where(~((std96 == 0) & (v == mean96)), 0.0)
    f["vol_zscore"] = zscore.clip(-5, 5)
    mean12 = v.rolling(12).mean()
    f["vol_trend_12_96"] = _ratio_frame(mean12, mean96)
    age, dist, conf = dc_leg_series(c.tolist())
    f["dc_age"] = age
    f["dc_dist"] = dist
    f["dc_confirmed"] = conf
    return f


def _ratio_frame(num, den):
    """Frame twin of _safe_ratio: both-zero -> 1.0, x/0 -> clip to 5."""
    out = num / den
    out = out.where(~((num == 0) & (den == 0)), 1.0)
    return out.clip(0, 5)


def live_vector(candles, *, asset: str):
    """Pure-python twin of the 22-feature vector at the last close.

    candles: list of MarketCandle (5-minute bars, oldest -> newest).
    Returns ``(values, flags)``: 22 values in FEATURES order and 5
    per-group availability flags, or None without enough history
    (fewer than WINDOW contiguous bars). Volcal has no optional groups:
    a returned vector always carries all five groups (flags all 1.0).
    The base 10 come from features.feature_vector (the incumbent's
    exact definitions); the new 12 are computed from the last 300 bars
    with the same formulas and flat-market limits as frame_new_features.
    """
    from parlay_bot.perpetuals.features import feature_vector

    if asset not in ("BTC", "ETH", "SOL", "XRP") or len(candles) < WINDOW:
        return None
    try:
        base = feature_vector(candles)
    except ValueError:
        return None
    window = candles[-WINDOW:]
    closes = [float(c.close_price) for c in window]
    highs = [float(c.high_price) for c in window]
    lows = [float(c.low_price) for c in window]
    vols = [float(c.volume_base) for c in window]
    logrets = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        logrets.append(math.log(cur / prev) if prev > 0 and cur > 0 else 0.0)
    # logrets[j] is the return of bar j+1; the trailing 288 end at the
    # last bar, i.e. logrets[-288:].
    vol288 = _pop_std(logrets[-DC_SIGMA_BARS:])
    vol72 = _pop_std(logrets[-72:])
    vol12 = _pop_std(logrets[-12:])
    ret1_raw = logrets[-1] * 100
    vol_24h = _clamp(vol288 * 100, 0.0, 5.0)
    values = list(base)
    values.append(vol_24h)
    values.append(_safe_ratio(vol72, vol288))
    values.append(_clamp(ret1_raw / max(vol288 * 100, 1e-6), -5.0, 5.0))
    values.append(_safe_ratio(vol12, vol288))
    logrange8 = sum(math.log(highs[i] / lows[i]) for i in range(-8, 0)) / 8
    logrange96 = sum(math.log(highs[i] / lows[i]) for i in range(-96, 0)) / 96
    values.append(_safe_ratio(logrange8, logrange96))
    meanabs8 = sum(abs(x) for x in logrets[-8:]) / 8
    meanabs96 = sum(abs(x) for x in logrets[-96:]) / 96
    values.append(_safe_ratio(meanabs8, meanabs96))
    med96 = _median(vols[-96:])
    if vols[-1] == 0 and med96 == 0:
        volz = 0.0
    elif med96 == 0:
        volz = 2.0
    else:
        volz = _clamp(math.log(vols[-1] / med96), -2.0, 2.0)
    values.append(volz)
    mean96 = sum(vols[-96:]) / 96
    std96 = _pop_std(vols[-96:])
    if std96 == 0.0:
        zscore = 0.0 if vols[-1] == mean96 else (5.0 if vols[-1] > mean96 else -5.0)
    else:
        zscore = _clamp((vols[-1] - mean96) / std96, -5.0, 5.0)
    values.append(zscore)
    mean12 = sum(vols[-12:]) / 12
    values.append(_safe_ratio(mean12, mean96))
    age, dist, conf = dc_leg_series(closes)
    values.append(age[-1])
    values.append(dist[-1])
    values.append(conf[-1])
    return values, (1.0,) * len(FLAG_NAMES)
