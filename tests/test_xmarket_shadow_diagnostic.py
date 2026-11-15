"""Read-only XMarket production diagnosis separates import, native-clock, and venue gates."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.xmarket_inputs import XmarketInputs
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
from parlay_bot.perpetuals import xmarket_shadow_diagnostic as diagnostic
from tests.xmarket_fixtures import NOW, point, valid_doc

TARGET = NOW.replace(second=0, microsecond=0)
CLOCK = TARGET + timedelta(minutes=1)


def _setup(store, *, venues=("bitget", "okx")):
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    XmarketInputs(store).initialize()
    models = XmarketModelSet(store)
    models.import_document("private-fit", json.dumps(
        valid_doc(venues=venues, horizon=15, now=CLOCK)).encode())
    return models


def _incumbent(store):
    ref = TARGET - timedelta(minutes=5)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO perp_forward_audit "
            "(venue,contract,quote,asset,horizon_minutes,model_version,"
            "reference_opened_at,reference_close,issued_at,target_opened_at,"
            "probability_up,baseline_up,training_evaluated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hyperliquid", "BTC", "USDC", "BTC", 15, "prospective-logistic-persistent-v3",
             ref.isoformat(), 10., (TARGET+timedelta(seconds=5)).isoformat(),
             (ref+timedelta(minutes=15)).isoformat(), .6, .5, 100),
        )


def _census(monkeypatch, *, complete=True):
    venues = ("bitget", "okx") if complete else ("bitget",)
    monkeypatch.setattr(diagnostic.ShadowLearningMonitor, "_eligible_streams",
                        lambda *_args, **_kwargs: [(venue, "BTC", "USDT") for venue in venues])


def test_imported_without_prospective_rows_is_not_called_shadow_ready(learning_store, monkeypatch):
    _setup(learning_store)
    _census(monkeypatch)
    report = diagnostic.read_probe(learning_store, CLOCK)
    assert report["read_only"] and report["does_not_prove_prospective_predictions"]
    item = report["scopes"][0]
    assert item["asset"] == "BTC" and item["horizon_minutes"] == 15
    assert item["stored_artifact_usable"] is True
    assert item["status"] == "no_recent_incumbent"
    assert item["recent_incumbent_rows"] == 0
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_xmarket_models").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts").fetchone()["n"] == 0


def test_native_source_partial_vs_complete_is_prospective_not_replayed(
    learning_store, monkeypatch,
):
    _setup(learning_store)
    _census(monkeypatch)
    _incumbent(learning_store)
    missing = diagnostic.read_probe(learning_store, CLOCK)["scopes"][0]
    assert missing["status"] == "missing_exact_native_pair_at_issuance"
    assert missing["native_at_issuance"]["native_complete"] == 0

    times = (TARGET, TARGET-timedelta(minutes=15), TARGET-timedelta(minutes=60))
    XmarketInputs(learning_store).record([
        point("polymarket", stamp) for stamp in times
    ] + [point("kalshi", TARGET)])
    complete = diagnostic.read_probe(learning_store, CLOCK)["scopes"][0]
    assert complete["recent_incumbent_rows"] == 1
    assert complete["native_at_issuance"] == {
        "pm_current": 1, "pm_15m_ago": 1, "pm_60m_ago": 1,
        "kalshi_current": 1, "native_complete": 1,
    }
    assert complete["status"] == "needs_candle_window_proof"
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts").fetchone()["n"] == 0


def test_missing_dispersion_venue_is_reported_without_relaxing_it(learning_store, monkeypatch):
    _setup(learning_store)
    _census(monkeypatch, complete=False)
    _incumbent(learning_store)
    XmarketInputs(learning_store).record([
        point("polymarket", stamp)
        for stamp in (TARGET, TARGET-timedelta(minutes=15), TARGET-timedelta(minutes=60))
    ] + [point("kalshi", TARGET)])
    result = diagnostic.read_probe(learning_store, CLOCK)["scopes"][0]
    assert result["status"] == "required_dispersion_venue_absent"
    assert result["missing_dispersion_venues"] == ["okx"]
    assert result["native_at_issuance"]["native_complete"] == 1


def test_missing_tables_or_invalid_host_data_are_not_passed_as_verified(learning_store):
    with pytest.raises(Exception):
        diagnostic.read_probe(learning_store, CLOCK)
