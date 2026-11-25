"""Defect sweep 2026-11-24 — residual fixes D-2, D-3, D-4.

D-2: a matured checkpoint sample first presented after a later-labelled
sample trained was silently skipped by the (label_known_at, issued_at)
cursor forever. Training order is unchanged; the skip is now counted and
logged instead of silent.
D-3: the opportunities desk labelled a probability of exactly 0.5 "DOWN";
the canonical direction rule (out_the_gate.direction_of) says FLAT.
D-4: the evidence receipt's per-source postgres detail called a failed
lineage read "no_records_found"; an unverified database read is a failed
read, not an empty database.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import evidence_board_receipt as receipt

from parlay_bot.discord_app.opportunities import recent_multi_watches, recent_watches
from parlay_bot.perpetuals import model_checkpoints as checkpoints_module
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.storage.hot import SQLiteHotStore

FEATURES = (.1, .2, .01, .03, .05, .2, .1, .1, .1, .1)


def _example(issued, known, label=1):
    return LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=issued, label_known_at=known, features=FEATURES, label_up=label)


# --- D-2 --------------------------------------------------------------------

def test_out_of_order_matured_sample_skip_is_counted_not_silent(tmp_path):
    hot = SQLiteHotStore(tmp_path / "cursor.sqlite")
    try:
        base = datetime.now(UTC).replace(microsecond=0)
        later = _example(base + timedelta(minutes=10), base + timedelta(minutes=20))
        earlier = _example(base, base + timedelta(minutes=15), label=0)
        learner = checkpoints_module.LiveModelCheckpoints(hot)
        first = learner.fit((later,), as_of=base + timedelta(minutes=30))
        assert first.updates == 1
        second = learner.fit((earlier, later), as_of=base + timedelta(minutes=30))
        # The earlier-labelled sample arrived too late for the cursor: it
        # still does not train (order unchanged) — but the skip is counted.
        assert second.updates == first.updates
        assert learner.summary()["skipped_untrained_this_process"] == 1
        # A normal in-order presentation counts nothing.
        nxt = _example(base + timedelta(minutes=25), base + timedelta(minutes=35))
        third = learner.fit((nxt,), as_of=base + timedelta(minutes=40))
        assert third.updates == first.updates + 1
        assert learner.summary()["skipped_untrained_this_process"] == 1
    finally:
        hot.close()


# --- D-3 --------------------------------------------------------------------

def _seeded_market(tmp_path, now, probability):
    store = SQLiteHotStore(tmp_path / "desk.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    opened = now - timedelta(minutes=6)
    observed = now - timedelta(minutes=1)
    candle = MarketCandle(
        "gate", "BTC_USDT", "USDT", 5, opened,
        opened + timedelta(minutes=5) - timedelta(milliseconds=1),
        observed, 100000, 100500, 99700, 100200, 3.5, 500)
    market.ingest(MarketBatch((), (candle,), 5, observed))
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=observed,
        new_candles=1, total_candles=1, last_candle_close=candle.closed_at)
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    with store.connect() as con:
        con.execute(
            """INSERT INTO perp_forward_audit(
                 venue,contract,quote,asset,horizon_minutes,model_version,
                 reference_opened_at,reference_close,issued_at,target_opened_at,
                 probability_up,baseline_up,training_evaluated
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("gate", "BTC_USDT", "USDT", "BTC", 5, MODEL,
             opened.isoformat(), 100200, observed.isoformat(),
             (now + timedelta(minutes=3)).isoformat(), probability, 0.5, 100))
    return store, market


def test_watch_at_exactly_fifty_fifty_is_flat_not_down(tmp_path):
    now = datetime(2026, 10, 29, 15, 30, tzinfo=UTC)
    store, market = _seeded_market(tmp_path, now, 0.5)
    try:
        items = recent_watches(market, asset="BTC", now=now)
        assert len(items) == 1
        assert items[0]["direction"] == "FLAT"
    finally:
        store.close()


def test_multi_watch_at_exactly_fifty_fifty_is_flat_not_down(tmp_path):
    now = datetime(2026, 10, 29, 15, 30, tzinfo=UTC)
    store, market = _seeded_market(tmp_path, now, 0.61)
    try:
        MultiExchangeResearch(store).initialize()
        observed = now - timedelta(minutes=1)
        with store.connect() as con:
            con.execute(
                """INSERT INTO perp_multi_forward(
                     asset,horizon_minutes,target_opened_at,issued_at,model_version,
                     probability_up,reference_probability_up,reference_venue,
                     reference_contract,reference_quote,reference_opened_at,
                     contributor_count,contributors_json,settled_at,outcome_up,
                     challenger_brier,incumbent_brier
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)""",
                ("BTC", 5, (now + timedelta(minutes=3)).isoformat(),
                 observed.isoformat(), MULTI_MODEL, 0.5, 0.5, "gate",
                 "BTC_USDT", "USDT", (now - timedelta(minutes=6)).isoformat(),
                 3, "[]"))
        items = recent_multi_watches(market, asset="BTC", now=now)
        assert len(items) == 1
        assert items[0]["direction"] == "FLAT"
    finally:
        store.close()


# --- D-4 --------------------------------------------------------------------

def test_receipt_postgres_detail_calls_a_failed_lineage_read_failed():
    host, db = receipt.summarize_host({
        "outcome": "verified",
        "forecast": {"status": "fresh", "issued": 3},
        "learning": {"status": "fresh"},
        "actual_learning": {"status": "read_unavailable"},
    })
    assert db["status"] == "read_unavailable"
    details = receipt._host_source_details(host, db, {}, "2026-11-24T00:00:00Z")
    assert details["postgres"]["status"] == "failed_read"
    assert details["postgres"]["reason"] == "read_unavailable"


def test_receipt_postgres_detail_not_recorded_lineage_is_a_verified_read():
    # "not_recorded" means the read worked and found no lineage yet: the
    # database leg stays verified, exactly as before the D-4 fix.
    host, db = receipt.summarize_host({
        "outcome": "verified",
        "forecast": {"status": "fresh", "issued": 3},
        "learning": {"status": "fresh"},
        "actual_learning": {"status": "not_recorded"},
    })
    assert db["status"] == "verified_read"
    details = receipt._host_source_details(host, db, {}, "2026-11-24T00:00:00Z")
    assert details["postgres"]["status"] == "ok"


def test_receipt_postgres_detail_stays_ok_when_verified():
    host = {"forecast_status": "fresh", "learning_status": "fresh"}
    db = {"status": "verified_read"}
    details = receipt._host_source_details(host, db, {}, "2026-11-24T00:00:00Z")
    assert details["postgres"]["status"] == "ok"
