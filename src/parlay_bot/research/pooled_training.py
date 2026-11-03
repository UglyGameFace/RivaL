"""Train RivaL's pooled multi-year model (offline, GitHub Actions only).

One LightGBM model per horizon, trained on every coin's Binance 5m history
together. Inputs are defined in parlay_bot.perpetuals.pooled_features and
re-implemented here with pandas; tests force both to agree.

Evaluation is strictly chronological (train < validation year < test). The
model is published only if it beats the basic up-rate forecast on validation
AND test, on most test days, with calibrated probabilities. The published
model is then refit on all data with the same fixed settings; its live
forward record is the final judge.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

from parlay_bot.perpetuals.pooled_features import ASSETS, FEATURE_VERSION, FEATURES, WINDOW
from parlay_bot.perpetuals.pooled_model import SCHEMA, probability, validate

PARAMS = {"n_estimators": 400, "learning_rate": 0.02, "num_leaves": 31, "max_depth": 6,
          "min_child_samples": 500, "subsample": 0.8, "subsample_freq": 1,
          "colsample_bytree": 0.8, "reg_lambda": 10.0, "n_jobs": 4,
          "random_state": 7, "verbosity": -1}
GATES = {"min_validation_gain": 0.0, "min_test_gain": 0.0,
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


def labels(k, horizon: int):
    import numpy as np
    import pandas as pd

    step = horizon // 5
    c = k["close"]
    future = c.shift(-step)
    ok = (k.index.to_series().shift(-step) - k.index.to_series()) == pd.Timedelta(minutes=horizon)
    y = (future > c).astype("float32")
    y[~ok.to_numpy() | future.isna().to_numpy()] = np.nan
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


def train_horizon(frames: dict, horizon: int, *, train_end, test_start, trained_through) -> dict:
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
    cols = list(FEATURES)
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
    # Refit on everything with the SAME fixed settings for the live model.
    final = lgb.LGBMClassifier(**PARAMS).fit(data[cols], data["y"])
    doc = {
        "schema": SCHEMA, "feature_version": FEATURE_VERSION, "features": list(FEATURES),
        "horizon_minutes": horizon, "family": "lightgbm",
        "params": PARAMS, "gates": GATES, "gates_passed": bool(passed),
        "trained_from": str(data.index.min()), "trained_through": str(trained_through),
        "training_rows": len(data), "evaluation": evaluation,
        "created_at": datetime.now(UTC).isoformat(),
        "trees": export_lightgbm(final),
    }
    validate(doc)
    # Exact-match check: the bot's evaluator must reproduce LightGBM itself.
    sample = test[cols].sample(min(2000, len(test)), random_state=1)
    native = final.predict_proba(sample)[:, 1]
    portable = np.array([probability(doc, tuple(float(x) for x in row))
                         for row in sample.itertuples(index=False)])
    error = float(np.max(np.abs(native - portable)))
    if not math.isfinite(error) or error > 1e-6:
        raise ValueError(f"portable evaluator disagrees with LightGBM by {error}")
    doc["portable_max_error"] = error
    return doc
