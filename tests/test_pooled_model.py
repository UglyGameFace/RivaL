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
        # A nightly retrain of the same recipe is stored and served, same name.
        retrain = _doc(through="2026-11-01T23:55:00+00:00")
        retrain["trees"] = [[[0, 0.0, 1, 2, 1, 2], [0.3], [-0.3]]]
        assert models.import_document("d", _json.dumps(retrain).encode()) == 1
        models._loaded_at = None
        served = models.active(5)
        assert served["trained_through"] == "2026-11-01T23:55:00+00:00"
        assert served["_family"] == active["_family"]
        assert served["_sha256"] != active["_sha256"]
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


def _context(n=700, seed=11, start=datetime(2026, 3, 1, tzinfo=UTC), funding_every=96):
    """Synthetic Binance context in both the live bundle and training-frame shapes."""
    from parlay_bot.research.pooled_training import funding_at_bars

    rng = np.random.default_rng(seed)
    closes = [start + timedelta(minutes=5 * (i + 1)) for i in range(n)]
    volume = rng.uniform(10, 100, n)
    volume[50] = 0.0  # zero-volume bar
    taker = volume * rng.uniform(0.2, 0.8, n)
    trades = rng.integers(0, 400, n).astype(float)
    premium = rng.normal(0, 3e-4, n)
    premium_map = {t: float(p) for t, p in zip(closes, premium)}
    del premium_map[closes[n - 100]]  # one missing premium bar
    funding_times = [start + timedelta(hours=8 * i, milliseconds=7) for i in range(n // 96 + 2)]
    rates = rng.normal(1e-4, 5e-5, len(funding_times))
    bundle = {
        "klines": [(t, float(v), float(b), float(c))
                   for t, v, b, c in zip(closes, volume, taker, trades)],
        "premium": premium_map,
        "funding": list(zip(funding_times, (float(r) for r in rates))),
    }
    index = pd.DatetimeIndex(closes)
    k = pd.DataFrame({"volume": volume, "taker_buy_volume": taker, "count": trades,
                      "prem": [premium_map.get(t, np.nan) for t in closes]}, index=index)
    k["fund"] = funding_at_bars(index, pd.DataFrame({
        "time": pd.DatetimeIndex(funding_times), "rate": rates}))
    return bundle, k, closes


def test_context_inputs_match_training_inputs():
    from parlay_bot.perpetuals.pooled_features import CONTEXT_FEATURES, context_vector
    from parlay_bot.research.pooled_training import frame_context

    bundle, k, closes = _context()
    trained = frame_context(k)
    checked = 0
    for end in (WINDOW, 480, len(closes) - 100, len(closes) - 50, len(closes)):
        live = context_vector(bundle, closes[end - 1])
        for name, got in zip(CONTEXT_FEATURES, live, strict=True):
            want = float(trained.iloc[end - 1][name])
            if math.isnan(want):
                assert math.isnan(got), (name, end)
            else:
                checked += 1
                assert got == pytest.approx(want, rel=1e-5, abs=1e-6), (name, end)
    assert checked > 30


def test_funding_counts_only_after_it_is_known():
    from parlay_bot.perpetuals.pooled_features import context_vector

    bundle, _, _ = _context()
    settle = bundle["funding"][5][0]  # 40 h in (past the 420-bar window), at :00.007
    at_settle = settle.replace(microsecond=0)
    before = context_vector(bundle, at_settle)[-1]
    after = context_vector(bundle, at_settle + timedelta(minutes=5))[-1]
    assert before == pytest.approx(bundle["funding"][4][1] * 1e4, rel=1e-5)
    assert after == pytest.approx(bundle["funding"][5][1] * 1e4, rel=1e-5)


def test_v2_models_never_predict_without_context():
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION_V2, model_vector

    coin = bars(seed=5)
    v2 = {"feature_version": FEATURE_VERSION_V2}
    assert model_vector(v2, coin, asset="BTC", context=None) is None
    assert len(model_vector({"feature_version": "x"}, coin, asset="BTC")) == len(FEATURES)


def test_v2_document_validates_with_wider_inputs():
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION_V2, FEATURES_V2

    last = len(FEATURES_V2) - 1
    doc = {"schema": "rival-pooled-model-v1", "feature_version": FEATURE_VERSION_V2,
           "features": list(FEATURES_V2), "horizon_minutes": 15,
           "trees": [[[last, 0.0, 1, 2, 1, 2], [0.4], [-0.4]]]}
    validate(doc)
    vector = [0.0] * len(FEATURES_V2)
    vector[last] = 1.0
    assert raw_score(doc, vector) == -0.4
    with pytest.raises(ValueError):
        probability(doc, [0.0] * len(FEATURES))


def test_warehouse_names_reuse_existing_collector_files():
    from parlay_bot.research.training_warehouse import OFFICIAL, warehouse_names

    assert warehouse_names(OFFICIAL + "monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2024-01.zip")[0] \
        == "BTCUSDT-5m-2024-01.zip"
    assert warehouse_names(
        OFFICIAL + "monthly/premiumIndexKlines/ETHUSDT/5m/ETHUSDT-5m-2024-02.zip")[0] \
        == "binance-aux-ETH-premium-2024-02--"
    assert warehouse_names(
        OFFICIAL + "monthly/fundingRate/SOLUSDT/SOLUSDT-fundingRate-2023-05.zip")[0] \
        == "binance-aux-SOL-funding-2023-05--"
    daily = warehouse_names(
        OFFICIAL + "daily/metrics/XRPUSDT/XRPUSDT-metrics-2024-03-01.zip")
    assert daily == ("rival-src__daily__metrics__XRPUSDT__XRPUSDT-metrics-2024-03-01.zip",)
    with pytest.raises(ValueError):
        warehouse_names("https://example.com/evil.zip")


def test_nightly_retrains_keep_one_live_record_per_recipe():
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION, FEATURE_VERSION_V2
    from parlay_bot.perpetuals.pooled_model import family, model_sha256

    base = {"schema": "rival-pooled-model-v1", "family": "lightgbm", "horizon_minutes": 5,
            "feature_version": FEATURE_VERSION, "features": list(FEATURES),
            "params": {"n_estimators": 400}, "trees": [[[0.1]]],
            "trained_through": "2026-11-01"}
    tomorrow = {**base, "trees": [[[0.2]]], "trained_through": "2026-11-02"}
    assert family(base) == family(tomorrow)
    assert model_sha256(base) != model_sha256(tomorrow)
    assert family({**base, "feature_version": FEATURE_VERSION_V2}) != family(base)
    assert family({**base, "horizon_minutes": 15}) != family(base)


def test_warehouse_fill_lists_depth_and_positioning_history():
    import importlib.util
    import pathlib
    import sys

    tools = pathlib.Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools))
    spec = importlib.util.spec_from_file_location("warehouse_fill", tools / "warehouse_fill.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from datetime import date

    urls = module.official_urls(date(2023, 1, 5))
    assert any("/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-2023-01-01.zip" in u for u in urls)
    assert any("/daily/metrics/XRPUSDT/XRPUSDT-metrics-2021-12-01.zip" in u for u in urls)
    assert not any("2023-01-04" in u for u in urls)  # newest two days not yet published


def test_price_only_model_serves_when_binance_context_is_unavailable(tmp_path):
    import json as _json

    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION_V2, FEATURES_V2
    from parlay_bot.perpetuals.pooled_model import PooledModelSet
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "fallback.sqlite")
    try:
        models = PooledModelSet(store)
        v1 = _doc(through="2026-10-31T23:55:00+00:00")
        v2 = {**_doc(through="2026-11-01T23:55:00+00:00"),
              "feature_version": FEATURE_VERSION_V2, "features": list(FEATURES_V2)}
        models.import_document("v1", _json.dumps(v1).encode())
        models.import_document("v2", _json.dumps(v2).encode())
        assert models.active(5)["feature_version"] == FEATURE_VERSION_V2
        assert models.active(5, context=False)["trained_through"] == v1["trained_through"]
    finally:
        store.close()


def test_unchanged_registry_reload_skips_documents_but_sees_new_and_maturing_models(tmp_path):
    """The 10-minute refresh re-downloaded ~10 MB of model JSON even when no
    model changed. It now reads only the (family, sha) list then, and still
    picks up a newly imported model and one whose training end was in the
    future at the last reload."""
    import json as _json
    from datetime import UTC, datetime

    from parlay_bot.perpetuals.pooled_model import PooledModelSet
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "reload.sqlite")
    try:
        models = PooledModelSet(store)
        models.import_document("a", _json.dumps(_doc(through="2026-10-30T23:55:00+00:00")).encode())
        future = _doc(through="2026-11-02T23:55:00+00:00")
        future["trees"] = [[[0, 0.0, 1, 2, 1, 2], [0.4], [-0.4]]]
        models.import_document("b", _json.dumps(future).encode())
        early = datetime(2026, 11, 1, tzinfo=UTC)
        later = datetime(2026, 11, 3, tzinfo=UTC)
        models._reload(early)
        assert models.active(5, now=early)["trained_through"].startswith("2026-10-30")
        assert models._pending_usable is True
        models._reload(later)  # same rows, but model b is now usable
        assert models.active(5, now=later)["trained_through"].startswith("2026-11-02")
        assert models._pending_usable is False

        selection = models._active
        models._reload(later)  # nothing changed: selection object kept as is
        assert models._active is selection
        newer = _doc(through="2026-11-02T23:59:00+00:00")
        newer["trees"] = [[[0, 0.0, 1, 2, 1, 2], [0.1], [-0.1]]]
        models.import_document("c", _json.dumps(newer).encode())
        models._reload(later)
        assert models._active is not selection
        assert models.active(5, now=later)["trained_through"] == "2026-11-02T23:59:00+00:00"
    finally:
        store.close()


def test_imported_file_ids_lists_drive_files_already_in_the_registry(tmp_path):
    import json as _json

    from parlay_bot.perpetuals.pooled_model import PooledModelSet
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "ids.sqlite")
    try:
        models = PooledModelSet(store)
        assert models.imported_file_ids() == frozenset()
        models.import_document("driveA", _json.dumps(_doc()).encode())
        assert models.imported_file_ids() == frozenset({"driveA"})
    finally:
        store.close()


def test_validator_rejects_trees_that_would_loop_forever():
    from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION

    def doc(tree):
        return {"schema": "rival-pooled-model-v1", "feature_version": FEATURE_VERSION,
                "features": list(FEATURES), "horizon_minutes": 5, "trees": [tree]}

    # Node 1 sends its left child back to node 1: raw_score would never stop.
    self_loop = [[0, 0.5, 1, 2, 1, 2], [0, 0.5, 1, 2, 1, 3], [0.1], [0.2]]
    # Node 2 points back up to the root.
    back_to_root = [[0, 0.5, 1, 2, 1, 2], [0.1], [0, 0.5, 1, 2, 3, 0], [0.2]]
    for bad in (self_loop, back_to_root):
        with pytest.raises(ValueError, match="invalid pooled split"):
            validate(doc(bad))
    # A float child index is also refused.
    with pytest.raises(ValueError, match="invalid pooled split"):
        validate(doc([[0, 0.5, 1, 2, 1.0, 2], [0.1], [0.2]]))
    # A real exported shape (children after their parent) still validates.
    validate(doc([[0, 0.5, 1, 2, 1, 2], [0.1], [0.2]]))


def test_contest_keeps_enrolled_artifact_while_new_nightly_model_arrives(learning_store):
    import json

    from parlay_bot.perpetuals.model_league import POLICY_START, Contest
    from parlay_bot.perpetuals.pooled_model import PooledModelSet, family, model_sha256

    contest = Contest(learning_store)
    contest.initialize(now=POLICY_START-timedelta(days=2))
    models = PooledModelSet(learning_store)
    old = _doc(through=(POLICY_START-timedelta(days=3)).isoformat())
    old_sha = model_sha256(old)
    assert models.import_document('old', json.dumps(old).encode()) == 1
    with learning_store.connect() as conn:
        conn.execute('INSERT INTO rival_poly_contest_models VALUES (?,?)',
                     (f'pooled:{family(old)}@{old_sha}', (POLICY_START-timedelta(days=1)).isoformat()))
    newer = _doc(through=(POLICY_START+timedelta(days=2)).isoformat())
    new_sha = model_sha256(newer)
    assert models.import_document('new', json.dumps(newer).encode()) == 1
    contenders = models.contest_contenders(5, now=POLICY_START+timedelta(days=3))
    assert {x['_sha256'] for x in contenders} == {old_sha, new_sha}
    assert models.active(5, now=POLICY_START+timedelta(days=3))['_sha256'] == new_sha
