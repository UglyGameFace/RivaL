"""The pooled model forecasts Polymarket contracts from data up to their start."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_pooled import PolymarketPooledForecaster
from parlay_bot.perpetuals.pooled_features import FEATURE_VERSION, FEATURES, WINDOW
from parlay_bot.perpetuals.pooled_model import PooledModelSet
from parlay_bot.storage.hot import SQLiteHotStore

START = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
CONTRACTS = {"okx": ("BTC-USDT-SWAP", "USDT"), "gate": ("BTC_USDT", "USDT"),
             "kucoin": ("XBTUSDTM", "USDT"), "dydx": ("BTC-USD", "USD")}


def seed(market, *, bars=WINDOW + 10):
    for venue, (contract, quote) in CONTRACTS.items():
        for i in range(bars):
            opened = START - timedelta(minutes=5 * (bars - i))
            observed = opened + timedelta(minutes=5, seconds=10)
            price = 100 + math.sin(i / 7) + i * 0.01
            candle = MarketCandle(
                venue, contract, quote, 5, opened,
                opened + timedelta(minutes=5, milliseconds=-1), observed,
                price, price + .2, price - .2, price + .05, 10.0 + i % 3, 20)
            market.ingest(MarketBatch((), (candle,), 5, observed))


def model(store):
    doc = {"schema": "rival-pooled-model-v1", "feature_version": FEATURE_VERSION,
           "features": list(FEATURES), "horizon_minutes": 15, "gates_passed": True,
           "trained_through": "2026-10-31T23:55:00+00:00", "training_rows": 10,
           "trees": [[[0, 0.0, 1, 2, 1, 2], [0.4], [-0.4]]]}
    models = PooledModelSet(store)
    models.import_document("f", json.dumps(doc).encode())
    return models


def test_forecast_uses_bars_up_to_contract_start_and_skips_dydx(tmp_path):
    store = SQLiteHotStore(tmp_path / "pm.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed(market)
        forecaster = PolymarketPooledForecaster(market, model(store))
        result = forecaster.forecast(asset="BTC", horizon=15, opens_at=START,
                                     now=START + timedelta(seconds=40))
        assert result is not None and result["venues"] == 3  # okx, gate, kucoin
        assert 0 < result["probability"] < 1
        # Too late into the window, or a horizon without a model: no forecast.
        assert forecaster.forecast(asset="BTC", horizon=15, opens_at=START,
                                   now=START + timedelta(minutes=6)) is None
        assert forecaster.forecast(asset="BTC", horizon=5, opens_at=START,
                                   now=START + timedelta(seconds=40)) is None
    finally:
        store.close()


def test_scorecard_counts_value_bets_at_market_price(tmp_path):
    store = SQLiteHotStore(tmp_path / "score.sqlite")
    learner = PolymarketOutcomeLearner(store)
    try:
        learner.initialize()
        now = datetime.now(UTC)
        rows = [("a", .60, .50, "UP"), ("b", .40, .52, "UP"), ("c", .51, .50, "DOWN")]
        with store.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS rival_polymarket_ai_outcomes ("
                         "slug TEXT PRIMARY KEY, condition_id TEXT, direction TEXT, "
                         "resolved_at TEXT, observed_at TEXT, receipt_sha256 TEXT, "
                         "receipt_json TEXT)")
            for slug, p, m, d in rows:
                conn.execute(
                    "INSERT INTO rival_polymarket_observations (slug,condition_id,asset,"
                    "horizon,resolution_source,expires_at,observed_at,features_json,"
                    "evidence_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                    (slug, "c" + slug, "BTC", 15, "src", (now + timedelta(hours=1)).isoformat(),
                     now.isoformat(), "[]", "e"))
                conn.execute(
                    "INSERT INTO rival_polymarket_model_forward (slug,model_version,issued_at,"
                    "weights_sha256,probability_up,market_baseline) VALUES (?,?,?,?,?,?)",
                    (slug, "pooled:pooled_15m_x", (now - timedelta(minutes=1)).isoformat(),
                     "s", p, m))
                conn.execute("INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
                             (slug, "c" + slug, d, now.isoformat(), now.isoformat(), "r", "{}"))
        card = learner.pooled_summary(asset="BTC", horizon=15)
        assert card["contracts"] == 3
        assert card["value_bets"] == 2           # a (UP at .50) and b (DOWN at .48)
        assert card["value_bet_wins"] == 1       # a won, b lost
        assert abs(card["value_bet_avg_return"] - ((1 - .50) + (0 - .48)) / 2) < 1e-9
    finally:
        store.close()


def test_learner_issues_pooled_forecast_once_per_contract(tmp_path):
    store = SQLiteHotStore(tmp_path / "once.sqlite")
    learner = PolymarketOutcomeLearner(store)
    calls = []

    class Fake:
        def forecast(self, **kwargs):
            calls.append(kwargs)
            return {"probability": .6, "family": "pooled_15m_x", "model_sha256": "a" * 64}

    try:
        learner.initialize()
        learner.pooled_forecaster = Fake()
        notice = SimpleNamespace(slug="s1", asset="BTC", horizon=15, issued=START,
                                 quote_at=START + timedelta(seconds=30),
                                 up_ask=.5, down_ask=.52)
        learner._issue_pooled(notice, START + timedelta(seconds=30))
        later = SimpleNamespace(**{**vars(notice), "quote_at": START + timedelta(minutes=3),
                                   "up_ask": .9, "down_ask": .12})
        learner._issue_pooled(later, START + timedelta(minutes=3))
        assert len(calls) == 1
        # A contract first seen minutes after its start is never compared.
        late = SimpleNamespace(**{**vars(later), "slug": "s2"})
        learner._issue_pooled(late, START + timedelta(minutes=3))
        assert len(calls) == 1
        with store.connect() as conn:
            row = conn.execute("SELECT model_version, probability_up FROM "
                               "rival_polymarket_model_forward").fetchone()
        assert (row["model_version"], row["probability_up"]) == ("pooled:pooled_15m_x", .6)
        with store.connect() as conn:
            baseline = conn.execute("SELECT market_baseline FROM "
                                    "rival_polymarket_model_forward").fetchone()[0]
        assert baseline == pytest.approx(.5 / 1.02)  # the price at the start
    finally:
        store.close()
