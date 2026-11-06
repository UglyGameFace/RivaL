"""Train RivaL's pooled multi-year model (offline, GitHub Actions only).

One LightGBM model per horizon, trained on every coin's Binance 5m history
together. Inputs are defined in parlay_bot.perpetuals.pooled_features and
re-implemented here with pandas; tests force both to agree.

Evaluation is strictly chronological (train < validation year < test). The
model is published only if it beats the basic up-rate forecast on validation
AND test, on most test days, with calibrated probabilities. The published
model is the evaluated fit itself — it is never refit on the test slice, so
the published evaluation block describes the shipped weights; its live
forward record is the final judge.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

from parlay_bot.perpetuals.pooled_features import (
    ASSETS,
    CONTEXT_FEATURES,
    FEATURE_SETS,
    FEATURE_VERSION,
    FEATURES,
    WINDOW,
)
from parlay_bot.perpetuals.pooled_model import SCHEMA, probability, validate

PARAMS = {"n_estimators": 400, "learning_rate": 0.02, "num_leaves": 31, "max_depth": 6,
          "min_child_samples": 500, "subsample": 0.8, "subsample_freq": 1,
          "colsample_bytree": 0.8, "reg_lambda": 10.0, "n_jobs": 4,
          "random_state": 7, "verbosity": -1}
GATES = {"min_validation_gain": 0.005, "min_test_gain": 0.005,
         "min_test_daily_positive": 0.55, "max_test_ece": 0.03}


def frame_features(k, btc, asset: str):
    """pandas twin of pooled_features.live_vector over a whole history.

    k: DataFrame indexed by bar CLOSE time with open/high/low/close/volume.
    """
    import numpy as np
    import pandas as pd

    c, h, lo, v = k["close"], k["high"], k["low"], k["volume"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=k.index)
    f["ret_1"] = lr * 100
    f["ret_12"] = np.log(c / c.shift(12)) * 100
    f["vol_12"] = lr.rolling(12).std() * 100
    f["range"] = (h - lo) / c * 100
    f["vol_ratio"] = np.log((v + 1) / (v.shift(1).rolling(12).mean() + 1))
    diff = c.diff()
    gain = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    f["rsi"] = ((100 - 100 / (1 + gain / loss.replace(0, np.nan))) - 50) / 10
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    f["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / c * 100
    f["boll_z"] = (c - c.rolling(20).mean()) / (2 * c.rolling(20).std(ddof=0))
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14).mean() / c * 100
    f["vol_regime"] = np.log((v.rolling(5).mean() + 1) / (v.rolling(40).mean() + 1))
    f["ret_3"] = np.log(c / c.shift(3)) * 100
    f["ret_48"] = np.log(c / c.shift(48)) * 100
    f["ret_288"] = np.log(c / c.shift(288)) * 100
    f["vol_288"] = lr.rolling(288).std() * 100
    f["vol_short_long"] = np.log((f["vol_12"] + 1e-6) / (f["vol_288"] + 1e-6))
    hours = k.index.hour + k.index.minute / 60
    f["to_funding"] = (8 - (hours % 8)) / 8
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    f["dow"] = k.index.dayofweek.astype(float)
    if asset == "BTC" or btc is None:
        f["btc_ret_1"] = f["btc_ret_12"] = f["rel_ret_12"] = np.nan
    else:
        b = btc["close"].reindex(k.index)
        f["btc_ret_1"] = np.log(b / b.shift(1)) * 100
        f["btc_ret_12"] = np.log(b / b.shift(12)) * 100
        f["rel_ret_12"] = f["ret_12"] - f["btc_ret_12"]
    f["coin"] = float(ASSETS.index(asset))
    f = f[list(FEATURES)].replace([np.inf, -np.inf], np.nan).astype("float32")
    # Indicators must not bridge a missing bar: blank rows whose 420-bar
    # window is not continuous, exactly like the live contiguity check.
    gap = k.index.to_series().diff() != pd.Timedelta(minutes=5)
    broken = gap.astype(int).rolling(WINDOW - 1, min_periods=1).sum() > 0
    f.loc[broken.to_numpy()] = np.nan
    return f


def frame_context(k):
    """pandas twin of pooled_features.context_vector over a whole history.

    k: Binance 5m frame indexed by bar CLOSE time with volume,
    taker_buy_volume, count, prem (premium close at that bar, NaN if absent)
    and fund (last settled funding known at that bar, see funding_known_at).
    """
    import numpy as np
    import pandas as pd

    v, tb, n, p = k["volume"], k["taker_buy_volume"], k["count"], k["prem"]
    f = pd.DataFrame(index=k.index)
    f["taker_imb"] = tb / v.replace(0, np.nan) - 0.5
    f["taker_imb_12"] = tb.rolling(12).sum() / v.rolling(12).sum() - 0.5
    f["trades_ratio"] = np.log((n + 1) / (n.rolling(288).mean() + 1))
    f["avg_trade_size"] = np.log((v / n.replace(0, np.nan))
                                 / (v.rolling(288).sum() / n.rolling(288).sum()))
    f["prem"] = p * 1e4
    f["prem_chg_3"] = (p - p.shift(3)) * 1e4
    f["prem_chg_12"] = (p - p.shift(12)) * 1e4
    f["prem_z"] = (p - p.rolling(288).mean()) / p.rolling(288).std()
    f["fund"] = k["fund"] * 1e4
    f = f[list(CONTEXT_FEATURES)].replace([np.inf, -np.inf], np.nan).astype("float32")
    gap = k.index.to_series().diff() != pd.Timedelta(minutes=5)
    broken = gap.astype(int).rolling(WINDOW - 1, min_periods=1).sum() > 0
    f.loc[broken.to_numpy()] = np.nan
    return f


def funding_at_bars(index, funding):
    """Last settled funding rate known at each bar close (merge_asof backward).

    funding: DataFrame with 'time' (UTC datetime) and 'rate'."""
    import pandas as pd

    known = funding.assign(
        known=funding["time"].dt.floor("min") + pd.Timedelta(minutes=1)
    ).sort_values("known")[["known", "rate"]]
    bars = pd.DataFrame({"t": index}).sort_values("t")
    merged = pd.merge_asof(bars, known, left_on="t", right_on="known", direction="backward")
    return pd.Series(merged["rate"].to_numpy(), index=merged["t"]).reindex(index)


def labels(k, horizon: int):
    import numpy as np
    import pandas as pd

    step = horizon // 5
    c = k["close"]
    future = c.shift(-step)
    ok = (k.index.to_series().shift(-step) - k.index.to_series()) == pd.Timedelta(minutes=horizon)
    y = (future > c).astype("float32")
    y[~ok.to_numpy() | future.isna().to_numpy()] = np.nan
    if "trades" in k.columns:
        # Same rule as live grading: a flat target bar with no trades is a
        # repeated price, not a real NOT UP, so it is withdrawn.
        flat = (k["trades"].shift(-step) == 0) & (future == c)
        y[flat.to_numpy()] = np.nan
    return y


def export_lightgbm(model) -> list[list[list]]:
    """LightGBM booster -> compact array trees for pooled_model.raw_score."""
    native = model.booster_.dump_model()
    if native.get("objective", "").split()[0] != "binary":
        raise ValueError("only binary LightGBM models are exportable")
    missing_codes = {"None": 0, "Zero": 1, "NaN": 2}
    trees = []
    for info in native["tree_info"]:
        nodes: list[list] = []

        def visit(node, nodes=nodes):
            index = len(nodes)
            nodes.append([])
            if "leaf_value" in node:
                nodes[index] = [float(node["leaf_value"])]
                return index
            if node.get("decision_type") != "<=":
                raise ValueError("unsupported LightGBM split")
            left = visit(node["left_child"])
            right = visit(node["right_child"])
            nodes[index] = [int(node["split_feature"]), float(node["threshold"]),
                            int(bool(node["default_left"])),
                            missing_codes[node.get("missing_type", "None")], left, right]
            return index

        visit(info["tree_structure"])
        trees.append(nodes)
    return trees


def metrics(p, y, base: float, index) -> dict:
    import numpy as np
    import pandas as pd

    brier = float(np.mean((p - y) ** 2))
    base_brier = float(np.mean((base - y) ** 2))
    day = pd.Series(((base - y) ** 2) - ((p - y) ** 2), index=index).groupby(index.date).mean()
    bins = np.minimum(9, (p * 10).astype(int))
    ece = sum(abs(p[bins == b].mean() - y[bins == b].mean()) * (bins == b).mean()
              for b in range(10) if (bins == b).any())
    confident = (p >= 0.55) | (p <= 0.45)
    return {
        "rows": len(y), "days": len(day),
        "brier_gain": round(base_brier - brier, 6),
        "accuracy": round(float(np.mean((p > 0.5) == (y == 1))), 4),
        "daily_positive_fraction": round(float((day > 0).mean()), 3),
        "ece": round(float(ece), 4),
        "confident_share": round(float(confident.mean()), 4),
        "confident_accuracy": (round(float(np.mean((p[confident] > 0.5) == (y[confident] == 1))), 4)
                               if confident.any() else None),
    }


def train_horizon(frames: dict, horizon: int, *, train_end, test_start, trained_through,
                  feature_version: str = FEATURE_VERSION) -> dict:
    """frames: asset -> (candles DataFrame, features DataFrame)."""
    import lightgbm as lgb
    import numpy as np
    import pandas as pd

    parts = []
    for k, f in frames.values():
        part = f.copy()
        part["y"] = labels(k, horizon)
        parts.append(part.dropna(subset=["y", "ret_12", "vol_12", "rsi"]))
    data = pd.concat(parts).sort_index()
    purge = pd.Timedelta(minutes=horizon)
    train = data[data.index < train_end - purge]
    val = data[(data.index >= train_end) & (data.index < test_start - purge)]
    test = data[data.index >= test_start]
    cols = list(FEATURE_SETS[feature_version])
    model = lgb.LGBMClassifier(**PARAMS).fit(train[cols], train["y"])
    base = float(train["y"].mean())
    evaluation = {
        "validation": metrics(model.predict_proba(val[cols])[:, 1], val["y"].to_numpy(), base, val.index),
        "test": metrics(model.predict_proba(test[cols])[:, 1], test["y"].to_numpy(), base, test.index),
        "train_rows": len(train), "train_before": str(train_end), "test_from": str(test_start),
    }
    v, t = evaluation["validation"], evaluation["test"]
    passed = (v["brier_gain"] > GATES["min_validation_gain"]
              and t["brier_gain"] > GATES["min_test_gain"]
              and t["daily_positive_fraction"] >= GATES["min_test_daily_positive"]
              and t["ece"] <= GATES["max_test_ece"])
    # Publish the EVALUATED fit, not a refit: the shipped weights are the
    # ones scored on the test slice, so the published evaluation block
    # describes the model that actually serves. A refit on everything
    # (including test) would leave the evaluation describing weights that
    # no longer exist — that artifact would have to be labelled
    # "backtest-only, shipped model unevaluated" and would not be usable.
    doc = {
        "schema": SCHEMA, "feature_version": feature_version, "features": cols,
        "horizon_minutes": horizon, "family": "lightgbm",
        "params": PARAMS, "gates": GATES, "gates_passed": bool(passed),
        "evaluation_status": "held-out evaluated",
        "trained_from": str(data.index.min()), "trained_through": str(trained_through),
        "training_rows": len(train), "evaluation": evaluation,
        "created_at": datetime.now(UTC).isoformat(),
        "trees": export_lightgbm(model),
    }
    validate(doc)
    # Exact-match check: the bot's evaluator must reproduce LightGBM itself.
    sample = test[cols].sample(min(2000, len(test)), random_state=1)
    native = model.predict_proba(sample)[:, 1]
    portable = np.array([probability(doc, tuple(float(x) for x in row))
                         for row in sample.itertuples(index=False)])
    error = float(np.max(np.abs(native - portable)))
    if not math.isfinite(error) or error > 1e-6:
        raise ValueError(f"portable evaluator disagrees with LightGBM by {error}")
    doc["portable_max_error"] = error
    return doc


def venue_frames(records: list[bytes], *, exclude=("binance",)) -> dict:
    """RivaL's own 5m exchange candles (Drive backups) -> per-stream frames.

    records: rival-records__perp_market_candles__<day>.ndjson.gz payloads.
    Returns {"venue|contract|quote": (k, features)} with k indexed by bar
    CLOSE time like the Binance frames. Alts take the same venue's BTC as
    their BTC lead, exactly as the live bot does. Missing bars stay missing:
    frame_features blanks any window that is not continuous.
    """
    import gzip
    import io
    import json

    import pandas as pd

    from parlay_bot.perpetuals.markets import asset_for_contract

    keep = ("venue", "contract", "quote", "interval_minutes", "opened_at", "open_price",
            "high_price", "low_price", "close_price", "volume_base", "trades")
    rows = []
    for payload in records:
        with gzip.open(io.BytesIO(payload), "rt", encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item.get("interval_minutes") == 5 and item.get("venue") not in exclude:
                    rows.append(tuple(item.get(k) for k in keep))
    if not rows:
        return {}
    data = pd.DataFrame(rows, columns=list(keep))
    data["asset"] = [asset_for_contract(str(c)) for c in data["contract"]]
    data = data[data["asset"].isin(ASSETS)]
    data["t"] = pd.to_datetime(data["opened_at"], utc=True, format="ISO8601") + pd.Timedelta(minutes=5)
    data = data.drop_duplicates(["venue", "contract", "quote", "t"]).sort_values("t")
    streams = {}
    for (venue, contract, quote), part in data.groupby(["venue", "contract", "quote"]):
        k = pd.DataFrame({"open": part["open_price"].astype(float).to_numpy(),
                          "high": part["high_price"].astype(float).to_numpy(),
                          "low": part["low_price"].astype(float).to_numpy(),
                          "close": part["close_price"].astype(float).to_numpy(),
                          "volume": part["volume_base"].astype(float).to_numpy(),
                          "trades": part["trades"].astype(float).to_numpy()},
                         index=pd.DatetimeIndex(part["t"].to_numpy(), name="t"))
        k = k[k["close"] > 0]
        if len(k) >= WINDOW:
            streams[(venue, contract, quote, part["asset"].iloc[0])] = k
    frames = {}
    for (venue, contract, quote, asset), k in streams.items():
        btc = None
        if asset != "BTC":
            same = [s for s in streams if s[0] == venue and s[3] == "BTC"]
            same.sort(key=lambda s: (s[2] != quote, s[1]))  # same quote first
            btc = streams[same[0]][["close"]] if same else None
        frames[f"{venue}|{contract}|{quote}"] = (k, frame_features(k, btc, asset))
    return frames


def _stack(frames: dict, horizon: int, cols: list[str]):
    import pandas as pd

    parts = []
    for k, f in frames.values():
        part = f[cols].copy()
        part["y"] = labels(k, horizon)
        parts.append(part.dropna(subset=["y", "ret_12", "vol_12", "rsi"]))
    return pd.concat(parts).sort_index() if parts else None


def _lightgbm_fit(x, y):
    import lightgbm as lgb

    model = lgb.LGBMClassifier(**PARAMS).fit(x, y)
    return lambda rows: model.predict_proba(rows)[:, 1]


def venue_holdout(binance: dict, venues: dict, horizon: int, *, holdout_days: int = 28,
                  feature_version: str = FEATURE_VERSION, fit=None) -> dict:
    """Does adding RivaL's own candles help predict RivaL's own venues?

    Both models train on everything before the holdout (purged by the
    horizon): A on Binance only, B on Binance + RivaL's venue candles. Both
    are scored on the last ``holdout_days`` of venue bars, which neither saw.
    Passes only if B beats A on average AND on at least half the days.
    """
    import numpy as np
    import pandas as pd

    fit = fit or _lightgbm_fit
    cols = list(FEATURE_SETS[feature_version])
    a_rows, v_rows = _stack(binance, horizon, cols), _stack(venues, horizon, cols)
    if a_rows is None or v_rows is None or v_rows.empty:
        return {"passed": False, "reason": "no venue candles"}
    start = v_rows.index.max().normalize() - pd.Timedelta(days=holdout_days)
    cut = start - pd.Timedelta(minutes=horizon)
    test = v_rows[v_rows.index >= start]
    a_train = a_rows[a_rows.index < cut]
    b_train = pd.concat([a_train, v_rows[v_rows.index < cut]])
    if test.empty or len(b_train) == len(a_train):
        return {"passed": False, "reason": "not enough venue history before the holdout"}
    y = test["y"].to_numpy()
    pa = fit(a_train[cols], a_train["y"])(test[cols])
    pb = fit(b_train[cols], b_train["y"])(test[cols])
    up = float(b_train["y"].mean())
    daily = pd.Series((pa - y) ** 2 - (pb - y) ** 2, index=test.index).groupby(test.index.date).mean()
    gain = float(np.mean((pa - y) ** 2) - np.mean((pb - y) ** 2))
    positive = float((daily > 0).mean())
    return {
        "holdout_from": str(start), "rows": len(test), "days": len(daily),
        "venue_rows_added": len(b_train) - len(a_train),
        "brier_binance_only": round(float(np.mean((pa - y) ** 2)), 6),
        "brier_with_venues": round(float(np.mean((pb - y) ** 2)), 6),
        "brier_up_rate": round(float(np.mean((up - y) ** 2)), 6),
        "paired_gain": round(gain, 6), "positive_days": round(positive, 3),
        "passed": bool(gain > 0 and positive >= 0.5 and len(daily) >= 14),
    }
