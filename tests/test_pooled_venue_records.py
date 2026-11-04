"""The six-year model also learns from RivaL's own exchange candles (Drive backups)."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

START = datetime(2026, 9, 1, tzinfo=UTC)


def _day_file(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.open(buf, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return buf.getvalue()


def _candles(venue, contract, quote, bars, *, momentum=0.0, seed=1, start=START, trades=7):
    rng = random.Random(seed)
    rows, price, last_up = [], 100.0, True
    for i in range(bars):
        up = rng.random() < (0.5 + (momentum if last_up else -momentum))
        price *= 1.001 if up else 0.999
        last_up = up
        opened = start + timedelta(minutes=5 * i)
        rows.append({"venue": venue, "contract": contract, "quote": quote,
                     "interval_minutes": 5, "opened_at": opened.isoformat(),
                     "closed_at": (opened + timedelta(minutes=5)).isoformat(),
                     "first_observed_at": opened.isoformat(), "open_price": price,
                     "high_price": price * 1.0005, "low_price": price * 0.9995,
                     "close_price": price, "volume_base": 10 + i % 7, "trades": trades})
    return rows


def test_venue_frames_use_each_venues_own_btc_and_skip_binance():
    from parlay_bot.research.pooled_training import venue_frames

    rows = (_candles("okx", "BTC-USDT-SWAP", "USDT", 600)
            + _candles("okx", "ETH-USDT-SWAP", "USDT", 600, seed=2)
            + _candles("binance", "BTCUSDT", "USDT", 600))  # trained from originals
    rows += rows[:5]  # a row present in two day files counts once
    frames = venue_frames([_day_file(rows[:700]), _day_file(rows[700:])])
    assert set(frames) == {"okx|BTC-USDT-SWAP|USDT", "okx|ETH-USDT-SWAP|USDT"}
    k, f = frames["okx|ETH-USDT-SWAP|USDT"]
    assert len(k) == 600 and k.index[0] == START + timedelta(minutes=5)  # bar CLOSE time
    assert f["btc_ret_1"].notna().any()  # okx BTC is ETH's BTC lead on okx
    assert f["coin"].dropna().eq(1.0).all()
    btc_frame = frames["okx|BTC-USDT-SWAP|USDT"][1]
    assert btc_frame["btc_ret_1"].isna().all()


def test_flat_no_trade_target_bars_are_withdrawn_like_live_grading():
    from parlay_bot.research.pooled_training import labels

    index = pd.date_range(START, periods=4, freq="5min")
    k = pd.DataFrame({"close": [1.0, 1.0, 2.0, 2.0], "trades": [3, 0, 4, 2]}, index=index)
    y = labels(k, 5)
    assert np.isnan(y.iloc[0])  # next bar flat with no trades: not a real NOT UP
    assert y.iloc[1] == 1.0 and y.iloc[2] == 0.0  # flat WITH trades is a real NOT UP
    assert labels(k.drop(columns="trades"), 5).iloc[0] == 0.0  # Binance frames unchanged


def _momentum_fit(x, y):
    up = x["ret_1"].fillna(0).to_numpy() > 0
    rates = {s: float(y[up == s].mean()) if (up == s).any() else float(y.mean())
             for s in (True, False)}
    return lambda rows: np.where(rows["ret_1"].fillna(0).to_numpy() > 0,
                                 rates[True], rates[False])


def test_holdout_passes_only_when_venue_candles_help_on_unseen_venue_bars():
    from parlay_bot.research.pooled_training import venue_frames, venue_holdout

    bars = 60 * 288  # 60 days
    venue_rows = _candles("okx", "BTC-USDT-SWAP", "USDT", bars, momentum=0.2, seed=3)
    binance_rows = _candles("okx", "BTC-USDT-SWAP", "USDT", bars, seed=4,
                            start=START - timedelta(days=60))
    venues = venue_frames([_day_file(venue_rows)])
    binance = {"BTC": venue_frames([_day_file(binance_rows)])["okx|BTC-USDT-SWAP|USDT"]}
    helped = venue_holdout(binance, venues, 5, holdout_days=20, fit=_momentum_fit)
    assert helped["passed"] and helped["paired_gain"] > 0 and helped["days"] >= 14
    assert helped["brier_with_venues"] < helped["brier_binance_only"]
    # Venue candles that carry no extra pattern must not pass.
    noise = venue_frames([_day_file(_candles("okx", "BTC-USDT-SWAP", "USDT", bars, seed=5))])
    assert not venue_holdout(binance, noise, 5, holdout_days=20, fit=_momentum_fit)["passed"]
    assert venue_holdout(binance, {}, 5, fit=_momentum_fit)["passed"] is False


def _doc(sources=None, through="2026-11-03T00:00:00+00:00", tree=0.2):
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION, FEATURES

    doc = {"schema": "rival-pooled-model-v1", "feature_version": FEATURE_VERSION,
           "features": list(FEATURES), "horizon_minutes": 5, "gates_passed": True,
           "trained_through": through, "training_rows": 10, "family": "lightgbm",
           "params": {"n_estimators": 400},
           "trees": [[[0, 0.0, 1, 2, 1, 2], [tree], [-tree]]]}
    if sources:
        doc["training_sources"] = sources
    return doc


def test_venue_trained_model_runs_beside_the_binance_model_not_instead(tmp_path):
    from parlay_bot.perpetuals.pooled_model import VENUE_SOURCE, PooledModelSet, recipe
    from parlay_bot.storage.hot import SQLiteHotStore

    assert "training_sources" not in recipe(_doc())  # existing family names unchanged
    store = SQLiteHotStore(tmp_path / "pooled.sqlite")
    try:
        models = PooledModelSet(store)
        models.import_document("a", json.dumps(_doc()).encode())
        newer = _doc(["binance_um_originals", VENUE_SOURCE],
                     through="2026-11-04T00:00:00+00:00", tree=0.3)
        models.import_document("b", json.dumps(newer).encode())
        served = models.serving(5)
        assert [d["trained_through"] for d in served] == [
            "2026-11-03T00:00:00+00:00", "2026-11-04T00:00:00+00:00"]
        assert served[0]["_family"] != served[1]["_family"]
        assert models.active(5)["trained_through"] == "2026-11-03T00:00:00+00:00"  # default
        assert models.active(5, lineage="venues")["_family"] == served[1]["_family"]
    finally:
        store.close()


def test_every_served_lineage_gets_its_own_shadow_call():
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
    from parlay_bot.perpetuals.pooled_features import FEATURES

    a = {**_doc(), "_family": "pooled_5m_a", "_sha256": "a" * 64}
    b = {**_doc(tree=0.3), "_family": "pooled_5m_b", "_sha256": "b" * 64}
    vector = (1.0,) * len(FEATURES)
    calls = ForwardAccuracyMonitor._pooled_calls(object(), 5, {5: [(a, vector), (b, vector)]})
    assert [c["family"] for c in calls] == ["pooled_5m_a", "pooled_5m_b"]
    single = ForwardAccuracyMonitor._pooled_calls(object(), 5, {5: (a, vector)})
    assert [c["family"] for c in single] == ["pooled_5m_a"]
    assert ForwardAccuracyMonitor._pooled_calls(None, 5, {5: [(a, vector)]}) == []


def test_warehouse_reads_rivals_own_daily_backups(tmp_path):
    from parlay_bot.research.training_warehouse import TrainingWarehouse

    day1 = _day_file(_candles("okx", "BTC-USDT-SWAP", "USDT", 3))
    files = {"f1": ("rival-records__perp_market_candles__2026-11-01.ndjson.gz", day1),
             "f2": ("rival-records__perp_market_contexts__2026-11-01.ndjson.gz", b"x"),
             "f3": ("rival-records__perp_market_candles__2026-11-02.ndjson.gz", day1)}

    class Response:
        def __init__(self, payload=None, content=b""):
            self._payload, self.content = payload, content

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        async def get(self, url, params=None, headers=None):
            if params.get("alt") == "media":
                return Response(content=files[url.rsplit("/", 1)[-1]][1])
            return Response({"files": [
                {"id": i, "name": n, "parents": ["hist"],
                 "md5Checksum": hashlib.md5(b, usedforsecurity=False).hexdigest()}
                for i, (n, b) in files.items()]})

    class Drive:
        DRIVE_URL = "https://drive"
        _client = Client()
        folders = type("F", (), {"history": "hist"})()

        async def _verify_folder_boundary(self):
            return None

        @staticmethod
        def _escape_query(value):
            return value

        async def _headers(self):
            return {}

    warehouse = TrainingWarehouse(Drive(), Path(tmp_path))
    got = asyncio.run(warehouse.records("perp_market_candles"))
    assert got == [day1, day1]  # only that table's files, oldest first
    assert warehouse.stats["records_perp_market_candles"] == 2
