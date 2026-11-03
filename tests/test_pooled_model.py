"""The pooled model's live inputs and evaluator must match training exactly."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.pooled_features import FEATURES, WINDOW, live_vector
from parlay_bot.perpetuals.pooled_model import probability, raw_score, validate

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


@dataclass
class Bar:
    opened_at: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume_base: float


def bars(n=700, seed=3, start=datetime(2026, 3, 1, tzinfo=UTC), drift=0.0):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(drift, 0.002, n)))
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Bar(start + timedelta(minutes=5 * i), float(o),
                       float(max(o, c) * 1.0005), float(min(o, c) * 0.9995), float(c),
                       float(rng.uniform(5, 50))))
    return out


def frame(rows):
    return pd.DataFrame({
        "open": [b.open_price for b in rows], "high": [b.high_price for b in rows],
        "low": [b.low_price for b in rows], "close": [b.close_price for b in rows],
        "volume": [b.volume_base for b in rows],
    }, index=pd.DatetimeIndex([b.opened_at + timedelta(minutes=5) for b in rows]))


@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_live_inputs_match_training_inputs(asset):
    from parlay_bot.research.pooled_training import frame_features

    coin = bars(seed=5)
    btc = bars(seed=9)
    trained = frame_features(frame(coin), frame(btc), asset)
    for end in (WINDOW, 550, len(coin)):
        live = live_vector(coin[:end], asset=asset, btc_candles=btc[:end])
        expected = trained.iloc[end - 1]
        for name, got in zip(FEATURES, live, strict=True):
            want = float(expected[name])
            if math.isnan(want):
                assert math.isnan(got), name
            else:
                assert got == pytest.approx(want, rel=1e-5, abs=1e-6), (name, end)


def test_live_inputs_refuse_short_or_broken_history():
    coin = bars()
    assert live_vector(coin[:WINDOW - 1], asset="ETH") is None
    broken = coin[:200] + coin[201:WINDOW + 50]
    assert live_vector(broken, asset="ETH") is None


def test_portable_evaluator_routes_missing_values_like_lightgbm():
    doc = {"schema": "rival-pooled-model-v1", "feature_version":
           __import__("parlay_bot.perpetuals.pooled_features",
                      fromlist=["FEATURE_VERSION"]).FEATURE_VERSION,
           "features": list(FEATURES), "horizon_minutes": 5,
           "trees": [[[0, 0.5, 1, 2, 1, 2], [0.3], [-0.2]]]}
    validate(doc)
    nan = float("nan")
    base = [0.0] * len(FEATURES)
    assert raw_score(doc, [1.0] + base[1:]) == -0.2
    assert raw_score(doc, [0.1] + base[1:]) == 0.3
    assert raw_score(doc, [nan] + base[1:]) == 0.3  # default_left for NaN
    assert probability(doc, [0.1] + base[1:]) == pytest.approx(1 / (1 + math.exp(-0.3)))


def test_exported_lightgbm_matches_native_predictions():
    lgb = pytest.importorskip("lightgbm")
    from parlay_bot.research.pooled_training import export_lightgbm

    rng = np.random.default_rng(1)
    x = rng.normal(size=(4000, len(FEATURES))).astype("float32")
    x[rng.random(x.shape) < 0.05] = np.nan
    y = (x[:, 0] + rng.normal(size=4000) * 2 > 0).astype(int)
    model = lgb.LGBMClassifier(n_estimators=30, num_leaves=7, verbosity=-1).fit(x, y)
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION
    doc = {"schema": "rival-pooled-model-v1", "feature_version": FEATURE_VERSION,
           "features": list(FEATURES), "horizon_minutes": 15, "trees": export_lightgbm(model)}
    validate(doc)
    native = model.predict_proba(x[:300])[:, 1]
    mine = [probability(doc, tuple(float(v) for v in row)) for row in x[:300]]
    assert max(abs(a - b) for a, b in zip(native, mine, strict=True)) < 1e-6


def _doc(horizon=5, passed=True, through="2026-10-31T23:55:00+00:00"):
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION
    return {"schema": "rival-pooled-model-v1", "feature_version": FEATURE_VERSION,
            "features": list(FEATURES), "horizon_minutes": horizon, "gates_passed": passed,
            "trained_through": through, "training_rows": 1234,
            "trees": [[[0, 0.0, 1, 2, 1, 2], [0.2], [-0.2]]]}


def test_registry_serves_newest_passing_model_only(tmp_path):
    import json as _json

    from parlay_bot.perpetuals.pooled_model import PooledModelSet
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "pooled.sqlite")
    try:
        models = PooledModelSet(store)
        failing = _json.dumps(_doc(passed=False, through="2026-11-01T00:00:00+00:00")).encode()
        older = _json.dumps(_doc(through="2026-09-30T23:55:00+00:00")).encode()
        newer = _json.dumps(_doc()).encode()
        assert PooledModelSet.is_pooled_document(newer)
        for name, payload in (("a", failing), ("b", older), ("c", newer)):
            models.import_document(name, payload)
        active = models.active(5)
        assert active["trained_through"] == "2026-10-31T23:55:00+00:00"
        assert active["_family"].startswith("pooled_5m_") and len(active["_sha256"]) == 64
        assert models.active(15) is None
        assert models.import_document("c", newer) == 0  # idempotent
    finally:
        store.close()


def test_pooled_prediction_enters_shadow_ledger(tmp_path):
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "shadow.sqlite")
    try:
        competition = ShadowModelCompetition(store)
        competition.initialize()
        now = datetime(2026, 11, 3, 12, 1, tzinfo=UTC)
        with store.connect() as conn:
            saved = competition.issue(
                conn, samples=[], features=(0.0,) * 10, now=now, venue="okx",
                contract="BTC-USDT-SWAP", quote="USDT", asset="BTC", horizon=5,
                reference_opened_at=now - timedelta(minutes=6),
                target_opened_at=now - timedelta(minutes=1), issued_at=now,
                incumbent_up=.55, baseline_up=.5,
                pooled=[{"family": "pooled_5m_abc", "probability": .58,
                         "model_sha256": "f" * 64, "training_rows": 99}],
            )
            row = conn.execute(
                "SELECT probability_up, training_sha256, training_count FROM "
                "rival_model_shadow_forecasts WHERE family='pooled_5m_abc'").fetchone()
        assert saved >= 1
        assert (row["probability_up"], row["training_sha256"], row["training_count"]) == (
            .58, "f" * 64, 99)
    finally:
        store.close()
