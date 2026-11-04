"""Offline experiment: do RivaL's unused data sources predict direction?

Research only. Nothing here is served live, promoted or traded.

Compares, under one strict chronological split per coin and horizon:
  * price_only  - the live model's kind of inputs (returns, volatility,
                  range, volume, RSI, MACD, Bollinger, ATR) on 5m candles
  * full        - price_only plus derivatives and flow data RivaL collects
                  but never trains on: premium index (basis), funding rate,
                  taker buy/sell imbalance, trade intensity, open interest and
                  positioning ratios, BTC lead for the other coins, and
                  time-of-day/week.

Every feature uses only data known at the close of the issuing 5m bar.
Labels: close(t + h) > close(t). Splits: train < 2025-01-01, validation 2025,
test 2026-01-01 onward, with an h-minute purge at each boundary. The model
family and settings are fixed in advance; the test set is scored once.

Source: data.binance.vision original monthly/daily files (checksum-verified),
the same originals RivaL archives to Drive.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

BASE = "https://data.binance.vision/data/futures/um"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)
TRAIN_END = pd.Timestamp("2025-01-01", tz="UTC")
TEST_START = pd.Timestamp("2026-01-01", tz="UTC")
FIRST_MONTH = "2020-01"
METRICS_FROM = date(2021, 12, 1)  # Binance metrics archive start


def months(first: str, last: str) -> list[str]:
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _maybe_unpublished(url: str) -> bool:
    """A recent daily (3 days) or monthly (~40 days) file may not be out yet,
    so its "not found" must be re-checked next run, never cached forever."""
    import re

    today = datetime.now(UTC).date()
    found = re.search(r"-(\d{4}-\d{2}-\d{2})\.zip$", url)
    if found:
        return date.fromisoformat(found.group(1)) >= today - timedelta(days=3)
    found = re.search(r"-(\d{4}-\d{2})\.zip$", url)
    if found:
        return date.fromisoformat(found.group(1) + "-01") >= (
            today - timedelta(days=40)).replace(day=1)
    return False


async def fetch(client: httpx.AsyncClient, url: str, cache: Path, sem) -> bytes | None:
    path = cache / hashlib.sha1(url.encode()).hexdigest()
    if path.exists():
        return path.read_bytes() or None
    async with sem:
        for attempt in range(4):
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    if not _maybe_unpublished(url):  # recent: re-check next run
                        path.write_bytes(b"")
                    return None
                resp.raise_for_status()
                raw = resp.content
                check = await client.get(url + ".CHECKSUM")
                if check.status_code == 200:
                    expected = check.text.split()[0].lower()
                    if hashlib.sha256(raw).hexdigest() != expected:
                        raise ValueError("checksum mismatch")
                path.write_bytes(raw)
                return raw
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(2 ** attempt)
    return None


def read_zip_csv(raw: bytes, names: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        member = zf.namelist()[0]
        text = zf.read(member)
    frame = pd.read_csv(io.BytesIO(text), header=None)
    first = str(frame.iloc[0, 0])
    if not first.replace(".", "").lstrip("-").isdigit():
        frame = frame.iloc[1:]
    frame = frame.iloc[:, : len(names)]
    frame.columns = names[: frame.shape[1]]
    return frame


KLINE = ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume"]


def recent_days(last_month: str, through_day: date | None) -> list[date]:
    """UTC days after the last complete month, up to through_day inclusive."""
    if through_day is None:
        return []
    y, m = map(int, last_month.split("-"))
    d = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    out = []
    while d <= through_day:
        out.append(d)
        d += timedelta(days=1)
    return out


async def load_asset(client, asset: str, last_month: str, cache: Path, sem,
                     fetcher=None, through_day: date | None = None) -> pd.DataFrame:
    get = fetcher or fetch
    sym = f"{asset}USDT"
    ms = months(FIRST_MONTH, last_month)
    kl_urls = [f"{BASE}/monthly/klines/{sym}/5m/{sym}-5m-{m}.zip" for m in ms]
    pr_urls = [f"{BASE}/monthly/premiumIndexKlines/{sym}/5m/{sym}-5m-{m}.zip" for m in ms]
    fu_urls = [f"{BASE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip" for m in ms]
    # The current month only exists as daily files. Use them in order and
    # stop at the first one not yet published: a gap would corrupt every
    # rolling feature that spans it.
    monthly_k = await asyncio.gather(*(get(client, u, cache, sem) for u in kl_urls))
    # Binance posts a month's file a few days after it ends; cover any
    # trailing months it has not posted yet with that month's daily files.
    daily_from = last_month  # daily files start the month AFTER this one
    while (through_day is not None and monthly_k and not monthly_k[-1]
           and len(monthly_k) > 1):
        monthly_k.pop()
        kl_urls.pop()
        pr_urls.pop()
        fu_urls.pop()
        ms.pop()
        daily_from = ms[-1]
    recent = recent_days(daily_from, through_day)
    if recent:
        daily = await asyncio.gather(*(get(client, u, cache, sem) for d in recent for u in (
            f"{BASE}/daily/klines/{sym}/5m/{sym}-5m-{d.isoformat()}.zip",
            f"{BASE}/daily/premiumIndexKlines/{sym}/5m/{sym}-5m-{d.isoformat()}.zip")))
        recent_k, recent_p = [], []
        for i, _ in enumerate(recent):
            k_raw, p_raw = daily[2 * i], daily[2 * i + 1]
            if not k_raw:
                break
            recent_k.append(k_raw)
            if p_raw:
                recent_p.append(p_raw)
        covered_through = recent[len(recent_k) - 1] if recent_k else None
    else:
        recent_k, recent_p, covered_through = [], [], None
    days = []
    d = METRICS_FROM
    end = (covered_through + timedelta(days=1) if covered_through is not None
           else date.fromisoformat(last_month + "-01") + timedelta(days=31))
    while d < min(end, datetime.now(UTC).date() - timedelta(days=1)):
        days.append(d)
        d += timedelta(days=1)
    me_urls = [f"{BASE}/daily/metrics/{sym}/{sym}-metrics-{x.isoformat()}.zip" for x in days]
    raws = [*monthly_k, *await asyncio.gather(*(get(client, u, cache, sem)
                                               for u in pr_urls + fu_urls + me_urls))]
    n1, n2, n3 = len(kl_urls), len(kl_urls) + len(pr_urls), len(kl_urls) + len(pr_urls) + len(fu_urls)
    kl = [read_zip_csv(r, KLINE) for r in [*raws[:n1], *recent_k] if r]
    pr = [read_zip_csv(r, KLINE[:5]) for r in [*raws[n1:n2], *recent_p] if r]
    fu = [read_zip_csv(r, ["calc_time", "funding_interval_hours", "last_funding_rate"])
          for r in raws[n2:n3] if r]
    me = [read_zip_csv(r, ["create_time", "symbol", "oi", "oi_value", "toptrader_ls_count",
                           "toptrader_ls_position", "ls_count", "taker_ls_vol"])
          for r in raws[n3:] if r]
    if not kl:
        raise SystemExit(f"no klines for {asset}")
    k = pd.concat(kl)
    for col in KLINE:
        k[col] = pd.to_numeric(k[col], errors="coerce")
    k = k.dropna(subset=["open_time", "close"]).drop_duplicates("open_time").sort_values("open_time")
    k["t"] = pd.to_datetime(k["open_time"].astype("int64"), unit="ms", utc=True) + pd.Timedelta(minutes=5)
    k = k.set_index("t")
    if pr:
        p = pd.concat(pr)
        p["open_time"] = pd.to_numeric(p["open_time"], errors="coerce")
        p["prem"] = pd.to_numeric(p["close"], errors="coerce")
        p = p.dropna(subset=["open_time"]).drop_duplicates("open_time")
        p["t"] = pd.to_datetime(p["open_time"].astype("int64"), unit="ms", utc=True) + pd.Timedelta(minutes=5)
        k = k.join(p.set_index("t")[["prem"]], how="left")
    else:
        k["prem"] = np.nan
    if fu:
        f = pd.concat(fu)
        f["calc_time"] = pd.to_numeric(f["calc_time"], errors="coerce")
        f["fund"] = pd.to_numeric(f["last_funding_rate"], errors="coerce")
        f = f.dropna(subset=["calc_time"]).sort_values("calc_time")
        # Same rule as live (pooled_features.funding_known_at).
        f["known"] = (pd.to_datetime(f["calc_time"].astype("int64"), unit="ms", utc=True)
                      .dt.floor("min") + pd.Timedelta(minutes=1))
        k = pd.merge_asof(k.reset_index().sort_values("t"), f[["known", "fund"]].sort_values("known"),
                          left_on="t", right_on="known", direction="backward").set_index("t")
        # Funding has no daily files: past the last published rate (8 h
        # interval) it is unknown, not the last value carried forward.
        k.loc[k.index > f["known"].max() + pd.Timedelta(hours=9), "fund"] = np.nan
    else:
        k["fund"] = np.nan
    if me:
        m = pd.concat(me)
        m["known"] = pd.to_datetime(m["create_time"], utc=True, errors="coerce") + pd.Timedelta(minutes=5)
        for col in ("oi", "toptrader_ls_position", "ls_count", "taker_ls_vol"):
            m[col] = pd.to_numeric(m[col], errors="coerce")
        m = m.dropna(subset=["known"]).sort_values("known")
        k = pd.merge_asof(k.reset_index().sort_values("t"),
                          m[["known", "oi", "toptrader_ls_position", "ls_count", "taker_ls_vol"]]
                          .rename(columns={"known": "mknown"}),
                          left_on="t", right_on="mknown", direction="backward",
                          tolerance=pd.Timedelta(minutes=30)).set_index("t")
    else:
        for col in ("oi", "toptrader_ls_position", "ls_count", "taker_ls_vol"):
            k[col] = np.nan
    return k


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def features(k: pd.DataFrame, btc: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str], list[str]]:
    c, h, lo, v = k["close"], k["high"], k["low"], k["volume"]
    lr = np.log(c).diff()
    f = pd.DataFrame(index=k.index)
    # price_only: the live model's kind of inputs.
    f["ret_1"] = lr * 100
    f["ret_12"] = np.log(c / c.shift(12)) * 100
    f["vol_12"] = lr.rolling(12).std() * 100
    f["range"] = (h - lo) / c * 100
    f["vol_ratio"] = np.log((v + 1) / (v.shift(1).rolling(12).mean() + 1))
    f["rsi"] = (rsi(c) - 50) / 10
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    f["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / c * 100
    mean20, std20 = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    f["boll_z"] = (c - mean20) / (2 * std20)
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14).mean() / c * 100
    f["vol_regime"] = np.log((v.rolling(5).mean() + 1) / (v.rolling(40).mean() + 1))
    price_cols = list(f.columns)
    # extra: unused sources.
    f["ret_3"] = np.log(c / c.shift(3)) * 100
    f["ret_48"] = np.log(c / c.shift(48)) * 100
    f["ret_288"] = np.log(c / c.shift(288)) * 100
    f["vol_288"] = lr.rolling(288).std() * 100
    f["vol_ratio_short_long"] = np.log((f["vol_12"] + 1e-6) / (lr.rolling(288).std() * 100 + 1e-6))
    taker = k["taker_buy_volume"] / v.replace(0, np.nan)
    f["taker_imb"] = taker - 0.5
    f["taker_imb_12"] = (k["taker_buy_volume"].rolling(12).sum() / v.rolling(12).sum()) - 0.5
    f["trades_ratio"] = np.log((k["count"] + 1) / (k["count"].rolling(288).mean() + 1))
    f["avg_trade_size"] = np.log((v / k["count"].replace(0, np.nan)) /
                                 (v.rolling(288).sum() / k["count"].rolling(288).sum()))
    f["prem"] = k["prem"] * 1e4
    f["prem_chg_3"] = (k["prem"] - k["prem"].shift(3)) * 1e4
    f["prem_chg_12"] = (k["prem"] - k["prem"].shift(12)) * 1e4
    f["prem_z"] = (k["prem"] - k["prem"].rolling(288).mean()) / k["prem"].rolling(288).std()
    f["fund"] = k["fund"] * 1e4
    hours = k.index.hour + k.index.minute / 60
    f["to_funding"] = (8 - (hours % 8)) / 8
    f["oi_chg_12"] = np.log(k["oi"] / k["oi"].shift(12))
    f["oi_chg_48"] = np.log(k["oi"] / k["oi"].shift(48))
    f["top_ls"] = np.log(k["toptrader_ls_position"])
    f["ls_count"] = np.log(k["ls_count"])
    f["taker_ls"] = np.log(k["taker_ls_vol"])
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    f["dow"] = k.index.dayofweek
    if btc is not None:
        b = btc["close"].reindex(k.index)
        f["btc_ret_1"] = np.log(b / b.shift(1)) * 100
        f["btc_ret_12"] = np.log(b / b.shift(12)) * 100
        f["rel_ret_12"] = f["ret_12"] - f["btc_ret_12"]
    f = f.replace([np.inf, -np.inf], np.nan).astype("float32")
    extra = [col for col in f.columns if col not in price_cols]
    return f, price_cols, extra


def labels(k: pd.DataFrame, horizon: int) -> pd.Series:
    step = horizon // 5
    c = k["close"]
    future = c.shift(-step)
    # Only label when the future bar is exactly `horizon` minutes later.
    ok = (k.index.to_series().shift(-step) - k.index.to_series()) == pd.Timedelta(minutes=horizon)
    y = (future > c).astype("float32")
    y[~ok | future.isna()] = np.nan
    return y


def metrics(p: np.ndarray, y: np.ndarray, base: float, idx: pd.DatetimeIndex) -> dict:
    brier = float(np.mean((p - y) ** 2))
    base_brier = float(np.mean((base - y) ** 2))
    conf = (p >= 0.55) | (p <= 0.45)
    day = pd.Series(((base - y) ** 2) - ((p - y) ** 2), index=idx).groupby(idx.date).mean()
    bins = np.minimum(9, (p * 10).astype(int))
    ece = sum(abs(p[bins == b].mean() - y[bins == b].mean()) * (bins == b).mean()
              for b in range(10) if (bins == b).any())
    return {
        "n": len(y), "days": len(day),
        "brier_gain": round(base_brier - brier, 6),
        "accuracy": round(float(np.mean((p > 0.5) == (y == 1))), 4),
        "daily_positive_fraction": round(float((day > 0).mean()), 3),
        "ece": round(float(ece), 4),
        "confident_share": round(float(conf.mean()), 4),
        "confident_accuracy": (round(float(np.mean((p[conf] > 0.5) == (y[conf] == 1))), 4)
                               if conf.any() else None),
    }


def fit_eval(train, val, test, cols, *, seed=7):
    import lightgbm as lgb

    params = {"n_estimators": 400, "learning_rate": 0.02, "num_leaves": 31, "max_depth": 6,
                  "min_child_samples": 500, "subsample": 0.8, "subsample_freq": 1,
                  "colsample_bytree": 0.8, "reg_lambda": 10.0, "n_jobs": 4,
                  "random_state": seed, "verbosity": -1}
    model = lgb.LGBMClassifier(**params)
    model.fit(train[cols], train["y"])
    base = float(train["y"].mean())
    out = {}
    for name, part in (("validation", val), ("test", test)):
        p = model.predict_proba(part[cols])[:, 1]
        out[name] = metrics(p, part["y"].to_numpy(), base, part.index)
    importance = sorted(zip(cols, model.booster_.feature_importance("gain")),
                        key=lambda x: -x[1])[:12]
    out["top_features"] = [[name, round(float(g), 1)] for name, g in importance]
    return out


def split(frame: pd.DataFrame, horizon: int):
    purge = pd.Timedelta(minutes=horizon)
    train = frame[frame.index < TRAIN_END - purge]
    val = frame[(frame.index >= TRAIN_END) & (frame.index < TEST_START - purge)]
    test = frame[frame.index >= TEST_START]
    return train, val, test


async def main_async(args) -> dict:
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(16)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        data = {}
        for asset in ASSETS:
            data[asset] = await load_asset(client, asset, args.last_month, cache, sem)
            print(f"loaded {asset}: {len(data[asset]):,} bars "
                  f"{data[asset].index.min()} .. {data[asset].index.max()}", flush=True)
    report = {"schema": "rival-feature-signal-experiment-v1",
              "created_at": datetime.now(UTC).isoformat(),
              "split": {"train_before": str(TRAIN_END), "test_from": str(TEST_START)},
              "research_only": True, "results": {}}
    pooled: dict[int, list] = {h: [] for h in HORIZONS}
    for asset in ASSETS:
        feats, price_cols, extra = features(data[asset], None if asset == "BTC" else data["BTC"])
        for horizon in HORIZONS:
            frame = feats.copy()
            frame["y"] = labels(data[asset], horizon)
            frame = frame.dropna(subset=["y"] + price_cols)
            frame["coin"] = ASSETS.index(asset)
            pooled[horizon].append(frame)
            train, val, test = split(frame, horizon)
            if min(len(train), len(val), len(test)) < 1000:
                continue
            all_cols = price_cols + [c for c in extra if c in frame.columns]
            entry = {"rows": {"train": len(train), "validation": len(val), "test": len(test)},
                     "price_only": fit_eval(train, val, test, price_cols),
                     "full": fit_eval(train, val, test, all_cols)}
            report["results"][f"{asset}/{horizon}m"] = entry
            print(json.dumps({f"{asset}/{horizon}m": {
                k: {"val": v["validation"]["brier_gain"], "test": v["test"]["brier_gain"],
                    "test_acc": v["test"]["accuracy"], "conf_acc": v["test"]["confident_accuracy"],
                    "conf_share": v["test"]["confident_share"]}
                for k, v in entry.items() if k != "rows"}}), flush=True)
    for horizon in HORIZONS:
        frame = pd.concat(pooled[horizon]).sort_index()
        cols = [c for c in frame.columns if c not in ("y",)]
        price = [c for c in cols if c in price_cols] + ["coin"]
        train, val, test = split(frame, horizon)
        entry = {"rows": {"train": len(train), "validation": len(val), "test": len(test)},
                 "price_only": fit_eval(train, val, test, price),
                 "full": fit_eval(train, val, test, cols)}
        report["results"][f"POOLED/{horizon}m"] = entry
        print(json.dumps({f"POOLED/{horizon}m": {
            k: {"val": v["validation"]["brier_gain"], "test": v["test"]["brier_gain"],
                "test_acc": v["test"]["accuracy"], "conf_acc": v["test"]["confident_accuracy"],
                "conf_share": v["test"]["confident_share"]}
            for k, v in entry.items() if k != "rows"}}), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default="binance-cache")
    parser.add_argument("--last-month", default="2026-10")
    parser.add_argument("--out", default="feature-signal-report.json")
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print("REPORT_JSON=" + json.dumps(report, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
