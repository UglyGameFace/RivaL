"""Display actual imports and new shadow rows without promotion-table lag or writes."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

from parlay_bot.discord_app.xmarket_status import read
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
from tests.xmarket_fixtures import NOW, valid_doc


def _setup(store, *, horizons=(15, 60), asset="BTC"):
    models = XmarketModelSet(store)
    models.initialize()
    ShadowModelCompetition(store).initialize()
    for h in horizons:
        models.import_document(f"private-model-{h}", json.dumps(valid_doc(
            asset=asset, horizon=h, now=NOW)).encode())
    with store.connect() as conn:
        conn.execute("UPDATE rival_xmarket_models SET imported_at=?",
                     ((NOW-timedelta(hours=2)).isoformat(),))


def _shadow(store, *, horizon=15, issued=None, settled=None, asset="BTC", family="xmarket_v1"):
    issued = issued or NOW-timedelta(minutes=1)
    with store.connect() as conn:
        conn.execute("INSERT INTO rival_model_shadow_forecasts "
            "(venue,contract,quote,asset,horizon_minutes,family,reference_opened_at,"
            "issued_at,target_opened_at,probability_up,incumbent_up,baseline_up,training_count,"
            "training_sha256,settled_at,outcome_up) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hyperliquid", asset, "USDC", asset, horizon, family, issued.isoformat(),
             issued.isoformat(), (issued+timedelta(minutes=horizon)).isoformat(), .6,.55,.5,100,
             "a"*64, settled.isoformat() if settled else None, 1 if settled else None))


def test_xmarket_lifecycle_is_not_hidden_by_an_unscored_five_minute_page(learning_store):
    _setup(learning_store)
    _shadow(learning_store)
    _shadow(learning_store, issued=NOW-timedelta(hours=1), settled=NOW-timedelta(minutes=30))
    _shadow(learning_store, horizon=60)
    # Other family/asset, old and future rows cannot inflate this coin's state.
    _shadow(learning_store, family="volcal_v1")
    _shadow(learning_store, asset="ETH")
    _shadow(learning_store, issued=NOW-timedelta(days=2))
    _shadow(learning_store, issued=NOW+timedelta(minutes=1))
    record = read(learning_store, asset="BTC", now=NOW)
    scopes = {x["horizon_minutes"]: x for x in record["scopes"]}
    assert scopes[5]["status"] == "no_qualifying_fit"
    assert scopes[15]["status"] == "shadowing"
    assert (scopes[15]["issued_24h"], scopes[15]["settled_24h"], scopes[15]["pending_24h"]) == (2,1,1)
    assert scopes[60]["issued_24h"] == 1 and scopes[60]["settled_24h"] == 0
    assert not record["proves_edge"]
    assert "private-model" not in json.dumps(record) and "weights" not in json.dumps(record)


def test_missing_first_forecast_and_old_forecasts_are_distinct(learning_store):
    _setup(learning_store)
    _shadow(learning_store, horizon=60, issued=NOW-timedelta(minutes=30))
    scopes = {x["horizon_minutes"]: x for x in read(learning_store, asset="BTC", now=NOW)["scopes"]}
    assert scopes[15]["status"] == "imported_no_shadows"
    assert scopes[60]["status"] == "shadow_stale"


def test_missing_tables_are_unavailable_instead_of_zero_imports(learning_store):
    result = read(learning_store, asset="BTC", now=NOW)
    assert result["status"] == "unavailable" and "scopes" not in result


def test_an_invalid_stored_fit_is_not_called_a_ready_model(learning_store):
    _setup(learning_store, horizons=(15,))
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_xmarket_models SET document='{}'")
    scopes = read(learning_store, asset="BTC", now=NOW)["scopes"]
    assert scopes[1]["status"] == "fit_unavailable"


def test_lifecycle_read_uses_selects_only_and_never_initializes_schema(learning_store):
    _setup(learning_store)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=()):
            assert sql.lstrip().upper().startswith("SELECT ")
            return self.conn.execute(sql, args)

    class ReadOnly:
        @contextmanager
        def connect(self):
            with learning_store.connect() as conn:
                yield Connection(conn)

    assert read(ReadOnly(), asset="BTC", now=NOW)["status"] == "recorded"
