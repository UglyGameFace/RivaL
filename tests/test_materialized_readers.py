"""Slice 1 PR-2: flag-gated bot readers over rival_materialized_reads.

Contract, per reader (forward summary / calibration / active-learning
evidence / Polymarket status builder):
- flag OFF: the lookup is never attempted and the result is exactly the
  live computation;
- flag ON + fresh row: the stored payload is served;
- flag ON + stale / missing / corrupt / no-table: the exact live
  computation serves instead;
- an explicit historical clock always computes live, flag or no flag.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage import materialized_reads as mr
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.fixture(autouse=True)
def _flag_cache():
    mr.reset_reads_enabled_cache()
    yield
    mr.reset_reads_enabled_cache()


def _flag(monkeypatch, on: bool) -> None:
    if on:
        monkeypatch.setenv("RIVAL_MATERIALIZED_READS", "true")
    else:
        monkeypatch.delenv("RIVAL_MATERIALIZED_READS", raising=False)
    mr.reset_reads_enabled_cache()


def _seed_settled_call(store, *, asset="BTC", horizon=5, p=0.7, up=1,
                       age=timedelta(hours=2)):
    issued = datetime.now(UTC) - age
    ref = (issued - timedelta(minutes=horizon)).isoformat()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit (venue,contract,quote,asset,horizon_minutes,
                 model_version,reference_opened_at,reference_close,issued_at,target_opened_at,
                 probability_up,baseline_up,training_evaluated,settled_at,outcome_up,
                 realized_close,model_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("okx", f"{asset}-USDT", "USDT", asset, horizon, MODEL, ref, 100.0,
             issued.isoformat(), issued.isoformat(), p, .5, 1,
             issued.isoformat(), up, 101.0, (p - up) ** 2, .25))


@pytest.fixture
def world(tmp_path):
    store = SQLiteHotStore(tmp_path / "readers.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    _seed_settled_call(store)
    yield store, monitor
    store.close()


def _without_as_of(payload):
    return {key: value for key, value in payload.items() if key != "as_of"}


def _live_summary(monitor, asset="BTC"):
    return _without_as_of(monitor._summary_uncached(asset=asset))


def test_flag_off_never_looks_up_and_matches_live(world, monkeypatch):
    _store, monitor = world
    _flag(monkeypatch, False)

    def forbidden(*args, **kwargs):
        raise AssertionError("materialized lookup attempted while flag off")

    # The wrappers import the name from the module at call time, so
    # patching the module attribute is enough to prove non-use.
    monkeypatch.setattr(mr, "lookup_if_enabled", forbidden)
    assert _without_as_of(monitor.summary(asset="BTC")) == _live_summary(monitor)
    live_cal = monitor._calibration_uncached(asset="BTC", horizon=5)
    assert monitor.calibration(asset="BTC", horizon=5) == live_cal
    live_ale = monitor._active_learning_evidence_uncached(
        asset="BTC", horizon_minutes=5)
    assert monitor.active_learning_evidence(
        asset="BTC", horizon_minutes=5) == live_ale


def test_flag_on_fresh_rows_are_served(world, monkeypatch):
    store, monitor = world
    _flag(monkeypatch, True)
    reads = mr.materialized_reads(store)
    wall = datetime.now(UTC)
    sentinels = {
        (mr.KIND_FORWARD_SUMMARY, "BTC", 0): {"sentinel": "summary"},
        (mr.KIND_FORWARD_CALIBRATION, "BTC", 5): {"sentinel": "calibration"},
        (mr.KIND_ACTIVE_LEARNING, "BTC", 5): {"sentinel": "active"},
    }
    for (kind, asset, horizon), payload in sentinels.items():
        reads.upsert(kind, asset, horizon, payload, computed_at=wall)
    assert monitor.summary(asset="BTC") == {"sentinel": "summary"}
    assert monitor.calibration(asset="BTC", horizon=5) == \
        {"sentinel": "calibration"}
    assert monitor.active_learning_evidence(asset="BTC", horizon_minutes=5) \
        == {"sentinel": "active"}


def test_flag_on_stale_missing_and_corrupt_fall_back(world, monkeypatch):
    store, monitor = world
    _flag(monkeypatch, True)
    reads = mr.materialized_reads(store)
    stale = datetime.now(UTC) - timedelta(
        seconds=mr.FRESHNESS_SECONDS + 60)
    reads.upsert(mr.KIND_FORWARD_SUMMARY, "BTC", 0, {"sentinel": "old"},
                 computed_at=stale)
    assert _without_as_of(monitor.summary(asset="BTC")) == _live_summary(monitor)
    # Missing row (ETH was never written).
    assert _without_as_of(monitor.summary(asset="ETH")) == \
        _without_as_of(monitor._summary_uncached(asset="ETH"))
    # Corrupt payload.
    reads.upsert(mr.KIND_FORWARD_CALIBRATION, "BTC", 5, {"sentinel": "x"},
                 computed_at=datetime.now(UTC))
    with store.connect() as conn:
        conn.execute(
            "UPDATE rival_materialized_reads SET payload_json='junk'"
            " WHERE kind=?", (mr.KIND_FORWARD_CALIBRATION,))
    live_cal = monitor._calibration_uncached(asset="BTC", horizon=5)
    assert monitor.calibration(asset="BTC", horizon=5) == live_cal


def test_flag_on_without_table_falls_back(tmp_path, monkeypatch):
    _flag(monkeypatch, True)
    store = SQLiteHotStore(tmp_path / "bare.sqlite")
    try:
        market = PerpetualMarketStore(store)
        market.initialize()
        monitor = ForwardAccuracyMonitor(market)
        monitor.initialize()
        # No rival_materialized_reads table exists in this store.
        assert _without_as_of(monitor.summary(asset="BTC")) == \
            _without_as_of(monitor._summary_uncached(asset="BTC"))
    finally:
        store.close()


def test_historical_clock_bypasses_materialized(world, monkeypatch):
    store, monitor = world
    _flag(monkeypatch, True)
    reads = mr.materialized_reads(store)
    reads.upsert(mr.KIND_FORWARD_SUMMARY, "BTC", 0, {"sentinel": "summary"},
                 computed_at=datetime.now(UTC))
    reads.upsert(mr.KIND_FORWARD_CALIBRATION, "BTC", 5,
                 {"sentinel": "calibration"}, computed_at=datetime.now(UTC))
    reads.upsert(mr.KIND_ACTIVE_LEARNING, "BTC", 5, {"sentinel": "active"},
                 computed_at=datetime.now(UTC))
    past = datetime.now(UTC) - timedelta(hours=1)
    assert _without_as_of(monitor.summary(asset="BTC", now=past)) == \
        _without_as_of(monitor._summary_uncached(asset="BTC", now=past))
    assert monitor.calibration(asset="BTC", horizon=5, now=past) == \
        monitor._calibration_uncached(asset="BTC", horizon=5, now=past)
    assert monitor.active_learning_evidence(
        asset="BTC", horizon_minutes=5, now=past) == \
        monitor._active_learning_evidence_uncached(
            asset="BTC", horizon_minutes=5, now=past)


def _status_client(store):
    from parlay_bot.discord_app.bot import RivalDiscordClient

    client = RivalDiscordClient.__new__(RivalDiscordClient)
    client.polymarket_research = PolymarketResearch(store)
    client.polymarket_research.initialize()
    client.kalshi_updown = None
    client.store = store
    return client


def test_polymarket_status_flag_off_matches_live(world, monkeypatch):
    store, _monitor = world
    _flag(monkeypatch, False)

    def forbidden(*args, **kwargs):
        raise AssertionError("materialized lookup attempted while flag off")

    monkeypatch.setattr(mr, "lookup_if_enabled", forbidden)
    client = _status_client(store)
    from parlay_bot.perpetuals.materialized_scorecards import polymarket_status_payload

    expected = polymarket_status_payload(
        client.polymarket_research.learner, asset="BTC", horizon=15)
    assert client.polymarket_learning_status("BTC", 15) == expected


def test_polymarket_status_flag_on_hit_and_miss(world, monkeypatch):
    store, _monitor = world
    _flag(monkeypatch, True)
    client = _status_client(store)
    reads = mr.materialized_reads(store)
    reads.upsert(mr.KIND_POLYMARKET_SUMMARY, "BTC", 15,
                 {"sentinel": "polymarket"}, computed_at=datetime.now(UTC))
    assert client.polymarket_learning_status("BTC", 15) == \
        {"sentinel": "polymarket"}
    # A scope with no row computes live.
    from parlay_bot.perpetuals.materialized_scorecards import polymarket_status_payload

    expected = polymarket_status_payload(
        client.polymarket_research.learner, asset="ETH", horizon=15)
    assert client.polymarket_learning_status("ETH", 15) == expected
