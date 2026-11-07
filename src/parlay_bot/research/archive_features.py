"""Archive-serving model (archive_v1) feature contract.

Spec: ~/workspace/your_files/rival-archive-serving-model-spec-20261107.md
§2.5, decisions D1/D5/D6/D14. Phase 1 only: training-time feature
definitions and their live twins. No serving path lives here.

The contract is ``archive-candle-premium-calendar-v1``: the incumbent 10
candle features, plus multi-horizon, calendar, cross-asset, original-premium
(where verified present), Binance funding, and the flagged Binance tape
group (Track C, §2.10.3). Up to 32 features.

Admissibility rule (§1.5): every feature is computable identically from
the archive (pandas frame, this module) and from live inputs at serve
time (pure-python ``live_vector``, this module). A group with no data
contributes a 0 availability flag, never a zero-filled value. Derived
premium is forbidden: premium features are NaN wherever the original
premium series is absent (notably the Nov 2024-Feb 2025 gap, which is a
missing-data region, never zero-filled).
"""

from __future__ import annotations

import hashlib
import json
import math

FEATURE_CONTRACT = "archive-candle-premium-calendar-v1"
FAMILY = "archive_v1"
MODEL_VERSION = "archive-offline-v1"
# Bars needed so the 288-bar windows are full and the exponential averages
# have converged to the training values (pooled precedent, WINDOW = 420).
WINDOW = 420

FEATURES = (
    # Incumbent 10 candle features (features.py feature_vector), same
    # formulas and clamps: ret_1, 5-bar return, 5-bar vol, range,
    # 5-bar volume regime, RSI, MACD histogram, Bollinger z, ATR,
    # 39-bar volume regime.
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
    # Multi-horizon candle features (pooled v1 precedent).
    "ret_3",
    "ret_48",
    "ret_288",
    "vol_288",
    "vol_short_long",
    # Calendar features (to_funding is a calendar quantity, not a rate).
    "hour_sin",
    "hour_cos",
    "dow",
    "to_funding",
    # Cross-asset features (pooled v1 precedent).
    "btc_ret_1",
    "btc_ret_12",
    "rel_ret_12",
    "coin",
    # Original premium only (pooled v2 precedent); NaN where absent.
    "prem",
    "prem_chg_3",
    "prem_chg_12",
    "prem_z",
    # Binance funding (pooled v2 precedent).
    "fund",
    # Binance tape group, Track C (§2.10.3): flagged, never assumed present.
    "taker_imb",
    "taker_imb_12",
    "trades_ratio",
    "avg_trade_size",
)
assert len(FEATURES) == 32

GROUPS = {
    "candle": tuple(range(10)),
    "multi_horizon": tuple(range(10, 15)),
    "calendar": tuple(range(15, 19)),
    "cross_asset": tuple(range(19, 23)),
    "premium": tuple(range(23, 27)),
    "funding": (27,),
    "binance_tape": tuple(range(28, 32)),
}
FLAG_NAMES = (
    "candle",
    "multi_horizon",
    "calendar",
    "cross_asset",
    "premium",
    "funding",
    "binance_tape",
)
# The Binance tape group is Binance-per-asset flow applied as market-wide
# context to all streams for that asset, not that venue's own tape.
BINANCE_TAPE_SCOPE = "binance-per-asset flow as market-wide context"

FEATURE_SOURCES = {
    name: source
    for name, source in [
        ("ret_1", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("ret_5", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("vol_5", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("range", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("vol_regime_5", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("rsi", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("macd_hist", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("boll_z", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("atr", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("vol_regime", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("ret_3", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("ret_48", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("ret_288", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("vol_288", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("vol_short_long", ("archive 5m candles", "live Postgres perp_market_candles")),
        ("hour_sin", ("bar close time (calendar)", "bar close time (calendar)")),
        ("hour_cos", ("bar close time (calendar)", "bar close time (calendar)")),
        ("dow", ("bar close time (calendar)", "bar close time (calendar)")),
        ("to_funding", ("bar close time (calendar)", "bar close time (calendar)")),
        ("btc_ret_1", ("archive BTC 5m candles", "live Postgres BTC candles")),
        ("btc_ret_12", ("archive BTC 5m candles", "live Postgres BTC candles")),
        ("rel_ret_12", ("archive BTC 5m candles", "live Postgres BTC candles")),
        ("coin", ("asset identity", "asset identity")),
        (
            "prem",
            (
                "Binance premiumIndexKlines, original only",
                "BinanceContextFeed premium (original only)",
            ),
        ),
        (
            "prem_chg_3",
            (
                "Binance premiumIndexKlines, original only",
                "BinanceContextFeed premium (original only)",
            ),
        ),
        (
            "prem_chg_12",
            (
                "Binance premiumIndexKlines, original only",
                "BinanceContextFeed premium (original only)",
            ),
        ),
        (
            "prem_z",
            (
                "Binance premiumIndexKlines, original only",
                "BinanceContextFeed premium (original only)",
            ),
        ),
        ("fund", ("Binance monthly fundingRate ZIPs", "BinanceContextFeed funding (last settled)")),
        (
            "taker_imb",
            ("Binance monthly kline ZIPs (taker_buy_volume)", "BinanceContextFeed klines"),
        ),
        (
            "taker_imb_12",
            ("Binance monthly kline ZIPs (taker_buy_volume)", "BinanceContextFeed klines"),
        ),
        ("trades_ratio", ("Binance monthly kline ZIPs (trade count)", "BinanceContextFeed klines")),
        (
            "avg_trade_size",
            ("Binance monthly kline ZIPs (volume/count)", "BinanceContextFeed klines"),
        ),
    ]
}

FEATURE_DIGEST = hashlib.sha256(
    json.dumps([FEATURE_CONTRACT, *FEATURES], separators=(",", ":")).encode()
).hexdigest()


def frame_features(k, btc=None, asset: str = "BTC"):
    """pandas twin of ``live_vector`` over a whole history.

    k: DataFrame indexed by bar CLOSE time with open/high/low/close/volume
    and optional trades, taker_buy_volume, count, prem, fund columns.
    btc: BTC close frame (same index) for cross-asset features, or None.
    Returns a DataFrame with exactly the 32 FEATURES columns. Rows whose
    420-bar window is not continuous are blanked, exactly like the pooled
    twin: indicators must not bridge a missing bar. A group with no data
    stays NaN (flag 0 at serve time), never zero-filled.
    """
    import numpy as np
    import pandas as pd

    from parlay_bot.research.pooled_training import frame_context
    from parlay_bot.research.pooled_training import frame_features as pooled_frame

    c, h, lo, v = k["close"], k["high"], k["low"], k["volume"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=k.index)
    # Incumbent 10, same formulas and clamps as features.feature_vector.
    f["ret_1"] = (lr * 100).clip(-5, 5)
    f["ret_5"] = (np.log(c / c.shift(5)) * 100).clip(-5, 5)
    f["vol_5"] = (lr.rolling(5).std(ddof=0) * 100).clip(0, 5)
    f["range"] = ((h - lo) / c * 100).clip(0, 5)
    f["vol_regime_5"] = np.log((v + 1) / (v.shift(1).rolling(5).mean() + 1)).clip(-2, 2)
    # RSI and MACD are window-local in the live definition
    # (features.indicator_features seeds its EMAs inside each 40-bar
    # window), so they are computed per window, exactly as live. The
    # other three indicators only read the last 20/14/39 bars, so the
    # vectorised rolling forms below are already window-local.
    from parlay_bot.perpetuals.features import indicator_features

    closes_l = c.tolist()
    highs_l = h.tolist()
    lows_l = lo.tolist()
    vols_l = v.tolist()
    rsi_vals = np.full(len(k), np.nan)
    macd_vals = np.full(len(k), np.nan)
    for i in range(39, len(k)):
        try:
            rsi_state, macd_state, _, _, _ = indicator_features(
                closes_l[i - 39 : i + 1],
                highs_l[i - 39 : i + 1],
                lows_l[i - 39 : i + 1],
                vols_l[i - 39 : i + 1],
            )
        except ValueError:
            continue
        rsi_vals[i] = rsi_state
        macd_vals[i] = macd_state
    f["rsi"] = rsi_vals
    f["macd_hist"] = macd_vals
    mean20 = c.rolling(20).mean()
    std20 = c.rolling(20).std(ddof=0)
    f["boll_z"] = ((c - mean20) / (2 * std20)).clip(-5, 5)
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = (tr.rolling(14).mean() / c * 100).clip(0, 5)
    f["vol_regime"] = np.log((v + 1) / (v.shift(1).rolling(39).mean() + 1)).clip(-2, 2)
    # Multi-horizon / calendar / cross-asset: the pooled twin, whose live
    # definitions are forced identical by tests/test_pooled_model.py.
    pooled = pooled_frame(k, btc, asset)
    for name in (
        "ret_3",
        "ret_48",
        "ret_288",
        "vol_288",
        "vol_short_long",
        "to_funding",
        "hour_sin",
        "hour_cos",
        "dow",
        "btc_ret_1",
        "btc_ret_12",
        "rel_ret_12",
        "coin",
    ):
        f[name] = pooled[name]
    # Premium / funding / Binance tape: the pooled context twin. Only
    # present when the source columns exist in k; otherwise all NaN.
    if {"taker_buy_volume", "count", "prem", "fund"} <= set(k.columns):
        context = frame_context(k)
        for name in (
            "prem",
            "prem_chg_3",
            "prem_chg_12",
            "prem_z",
            "fund",
            "taker_imb",
            "taker_imb_12",
            "trades_ratio",
            "avg_trade_size",
        ):
            f[name] = context[name]
    else:
        for name in (
            "prem",
            "prem_chg_3",
            "prem_chg_12",
            "prem_z",
            "fund",
            "taker_imb",
            "taker_imb_12",
            "trades_ratio",
            "avg_trade_size",
        ):
            f[name] = np.nan
    f = f[list(FEATURES)].replace([np.inf, -np.inf], np.nan).astype("float32")
    gap = k.index.to_series().diff() != pd.Timedelta(minutes=5)
    broken = gap.astype(int).rolling(WINDOW - 1, min_periods=1).sum() > 0
    f.loc[broken.to_numpy()] = np.nan
    return f


def live_vector(candles, *, asset: str, btc_candles=None, context: dict | None = None):
    """Pure-python twin of ``frame_features`` at the last candle's close.

    Returns ``(values, flags)``: 32 values in FEATURES order and 7
    per-group availability flags, or None without enough history
    (fewer than WINDOW contiguous bars). A group with no data contributes
    NaN values and a 0 flag, never a zero-filled value.
    """

    from parlay_bot.perpetuals.features import feature_vector
    from parlay_bot.perpetuals.pooled_features import (
        FEATURES as _PF,
    )
    from parlay_bot.perpetuals.pooled_features import (
        bar_close,
        context_vector,
    )
    from parlay_bot.perpetuals.pooled_features import (
        live_vector as pooled_live,
    )

    if asset not in ("BTC", "ETH", "SOL", "XRP") or len(candles) < WINDOW:
        return None
    try:
        incumbent = feature_vector(candles)
    except ValueError:
        return None
    pooled = pooled_live(candles, asset=asset, btc_candles=btc_candles)
    if pooled is None:
        return None
    by_name = dict(zip(_PF, pooled))
    close_at = bar_close(candles[-1])
    ctx = context_vector(context, close_at) if context is not None else None
    ctx_names = (
        "taker_imb",
        "taker_imb_12",
        "trades_ratio",
        "avg_trade_size",
        "prem",
        "prem_chg_3",
        "prem_chg_12",
        "prem_z",
        "fund",
    )
    ctx_by_name = dict(zip(ctx_names, ctx)) if ctx is not None else {}
    values: dict[str, float] = {}
    values.update(
        {
            "ret_1": incumbent[0],
            "ret_5": incumbent[1],
            "vol_5": incumbent[2],
            "range": incumbent[3],
            "vol_regime_5": incumbent[4],
            "rsi": incumbent[5],
            "macd_hist": incumbent[6],
            "boll_z": incumbent[7],
            "atr": incumbent[8],
            "vol_regime": incumbent[9],
        }
    )
    for name in (
        "ret_3",
        "ret_48",
        "ret_288",
        "vol_288",
        "vol_short_long",
        "to_funding",
        "hour_sin",
        "hour_cos",
        "dow",
        "btc_ret_1",
        "btc_ret_12",
        "rel_ret_12",
        "coin",
    ):
        values[name] = by_name[name]
    for name in (
        "prem",
        "prem_chg_3",
        "prem_chg_12",
        "prem_z",
        "fund",
        "taker_imb",
        "taker_imb_12",
        "trades_ratio",
        "avg_trade_size",
    ):
        values[name] = ctx_by_name.get(name, math.nan)
    ordered = tuple(values[name] for name in FEATURES)
    flags = tuple(
        1.0 if any(math.isfinite(ordered[i]) for i in idxs) else 0.0 for idxs in GROUPS.values()
    )
    return ordered, flags
