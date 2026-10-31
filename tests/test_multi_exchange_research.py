"""Production-shaped tests for the 10-source challenger and paired cost proof."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.multi_proof import multi_proof_embed
from parlay_bot.perpetuals.forward_audit import MODEL as BASE_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.multi_exchange import (
    MIN_QUORUM,
    MODEL,
    MultiExchangeResearch,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

CONTRACTS = {
    "hyperliquid": ("BTC", "USDC"),
    "dydx": ("BTC-USD", "USD"),
    "bitget": ("BTCUSDT", "USDT"),
    "gate": ("BTC_USDT", "USDT"),
    "okx": ("BTC-USDT-SWAP", "USDT"),
    "kucoin": ("XBTUSDTM", "USDT"),
    "deribit": ("BTC_USDC-PERPETUAL", "USDC"),
    "binance": ("BTCUSDT", "USDT"),
    "lighter": ("BTC", "USD"),
    "phemex": ("BTCUSDT", "USDT"),
}


def seed(store, now, *, venues=tuple(CONTRACTS), target=None, good=True):
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    if target is None:
        target = now + timedelta(minutes=2)
    for number, venue in enumerate(venues):
        contract, quote = CONTRACTS[venue]
        issued = now - timedelta(seconds=30)
        ref_open = now - timedelta(minutes=5)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,
                    target_opened_at,probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, contract, quote, "BTC", 5, BASE_MODEL,
                 ref_open.isoformat(), 100.0, issued.isoformat(),
                 target.isoformat(), .62 + number*.012, .5, 90),
            )
        market = PerpetualMarketStore(store)
        market.record_feed_health(
            venue=venue, asset="BTC",
            status="ok" if good else "blocked",
            attempted_at=now, last_candle_close=now-timedelta(minutes=5),
            new_candles=0, total_candles=1,
        )
    return target


def book(at, bid=100, ask=100.1):
    return PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=bid, ask=ask,
        bid_quantity=1000, ask_quantity=1000,
        exchange_at=at, received_at=at,
    )


def settle_anchor(store, now, target, *, outcome=1):
    with store.connect() as conn:
        conn.execute(
            """UPDATE perp_forward_audit
               SET outcome_up=?,settled_at=?,model_brier=?,baseline_brier=?
               WHERE venue='hyperliquid' AND target_opened_at=?""",
            (outcome,(now+timedelta(minutes=8)).isoformat(),
             (0.62-outcome)**2,(.5-outcome)**2,
             target.isoformat()),
        )


def test_all_ten_actual_source_forecasts_fuse_into_one_frozen_prediction(tmp_path):
    store = SQLiteHotStore(tmp_path / "ten.sqlite")
    now = datetime.now(UTC).replace(microsecond=0)
    target = seed(store, now)
    challenger = MultiExchangeResearch(store)
    try:
        assert challenger.issue(now=now) == 1
        assert challenger.issue(now=now) == 0
        with store.connect() as conn:
            row = conn.execute("SELECT * FROM perp_multi_forward").fetchone()
        assert row["model_version"] == MODEL
        assert row["contributor_count"] == 10
        assert len(json.loads(row["contributors_json"])) == 10
        assert sum(x["weight"] for x in json.loads(row["contributors_json"])) == pytest.approx(1)
        assert row["reference_venue"] == "hyperliquid"
        assert row["reference_quote"] == "USDC"
        assert row["target_opened_at"] == target.isoformat()
        assert row["issued_at"] == now.isoformat()
        assert row["probability_up"] > .5
        assert challenger.report(asset="BTC", horizon_minutes=5,now=now)["settled"] == 0
        rebooted = MultiExchangeResearch(SQLiteHotStore(tmp_path / "ten.sqlite"))
        assert rebooted.issue(now=now) == 0
        assert rebooted.report(asset="BTC",horizon_minutes=5,now=now)["issued"] == 1
    finally:
        store.close()


def test_missing_or_bad_source_quorum_fails_closed(tmp_path):
    store = SQLiteHotStore(tmp_path / "quorum.sqlite")
    now = datetime.now(UTC)
    challenger = MultiExchangeResearch(store)
    try:
        seed(store, now, venues=("hyperliquid", "binance"),good=True)
        assert challenger.issue(now=now) == 0
        seed(store, now, venues=("dydx",),good=False)
        assert challenger.issue(now=now) == 0
        assert MIN_QUORUM == 3
        assert challenger.report(asset="BTC",horizon_minutes=5,now=now)["issued"] == 0
    finally:
        store.close()


def test_no_future_leakage_no_stale_quotes_or_mismatched_time_windows(tmp_path):
    store = SQLiteHotStore(tmp_path / "causal.sqlite")
    now = datetime.now(UTC)
    try:
        target = seed(store,now,venues=("hyperliquid","binance","okx"))
        with store.connect() as conn:
            # Already-settled forecasts have no causal standing as new inputs.
            conn.execute(
                "UPDATE perp_forward_audit SET settled_at=? WHERE venue='okx'",
                ((now-timedelta(seconds=1)).isoformat(),),
            )
        a = MultiExchangeResearch(store)
        assert a.issue(now=now) == 0
        with store.connect() as conn:
            conn.execute("UPDATE perp_forward_audit SET settled_at=NULL WHERE venue='okx'")
        assert a.issue(now=now) == 1
        assert a.settle(now=now) == 0
        # Outcomes recorded after the issue date only, never before.
        settle_anchor(store,now,target)
        assert a.settle(now=now+timedelta(minutes=9)) == 1
        assert a.settle(now=now+timedelta(minutes=9)) == 0
        results = a.report(asset="BTC",horizon_minutes=5,now=now+timedelta(minutes=9))
        assert results["settled"] == 1
        assert results["challenger_brier"] is not None
        assert results["incumbent_brier"] == pytest.approx((.62-1)**2)
        assert not results["replacement_authorized"]
    finally:
        store.close()


def test_same_book_compares_both_models_after_spread_and_costs(tmp_path):
    store = SQLiteHotStore(tmp_path / "quotes.sqlite")
    now = datetime.now(UTC).replace(microsecond=0)
    target = seed(store,now,venues=("hyperliquid","binance","okx","dydx"))
    a = MultiExchangeResearch(store)
    try:
        assert a.issue(now=now) == 1
        entry = now+timedelta(seconds=20)
        exit_time = target+timedelta(minutes=5,seconds=20)
        assert a.observe(book(entry),now=entry)["opened"] == 1
        assert a.observe(book(entry),now=entry)["opened"] == 0
        assert a.observe(book(exit_time,bid=101,ask=101.1),now=exit_time)["closed"] == 1
        assert a.observe(book(exit_time,bid=101,ask=101.1),now=exit_time)["closed"] == 0
        settle_anchor(store, now, target)
        assert a.settle(now=exit_time+timedelta(minutes=1)) == 1
        proof = a.report(asset="BTC",horizon_minutes=5,now=exit_time+timedelta(minutes=1))
        assert proof["quote_pairs"] == 1
        assert proof["quote_missed"] == 0
        assert proof["after_cost"]["conservative"]["challenger_mean_net_bps"] is not None
        assert proof["after_cost"]["severe"]["incumbent_mean_net_bps"] is not None
        assert proof["after_cost"]["severe"]["challenger_mean_net_bps"] < (
            proof["after_cost"]["conservative"]["challenger_mean_net_bps"]
        )
        assert not proof["replacement_authorized"]
        visual=multi_proof_embed(proof)
        assert "Incumbent still active" in visual.description
        assert "Same-market after-cost observations" in [f.name for f in visual.fields]
    finally:
        store.close()


def test_missing_exit_is_never_fabricated_from_future_candle(tmp_path):
    store = SQLiteHotStore(tmp_path / "missed.sqlite")
    now = datetime.now(UTC)
    target = seed(store,now,venues=("hyperliquid","binance","dydx"))
    a = MultiExchangeResearch(store)
    try:
        assert a.issue(now=now) == 1
        assert a.observe(book(now+timedelta(seconds=10)),
                         now=now+timedelta(seconds=10))["opened"] == 1
        assert a.expire(now=now+timedelta(minutes=10)) == 1
        settle_anchor(store, now, target)
        assert a.settle(now=now+timedelta(minutes=10)) == 1
        proof = a.report(asset="BTC",horizon_minutes=5,now=now+timedelta(minutes=10))
        assert proof["quote_pairs"] == 0
        assert proof["quote_missed"] == 1
        assert proof["after_cost"]["conservative"]["challenger_mean_net_bps"] is None
    finally:
        store.close()


def test_postgres_causal_fusion_persists_on_restart():
    dsn=os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL16 integration service unavailable")
    store=PostgresHotStore(dsn)
    now=datetime.now(UTC).replace(microsecond=0)
    target=seed(store,now,venues=("hyperliquid","binance","okx"))
    try:
        first=MultiExchangeResearch(store)
        assert first.issue(now=now) == 1
        restarted=MultiExchangeResearch(PostgresHotStore(dsn))
        assert restarted.issue(now=now) == 0
        settle_anchor(store,now,target)
        assert restarted.settle(now=now+timedelta(minutes=9)) == 1
        assert restarted.report(
            asset="BTC",horizon_minutes=5,now=now+timedelta(minutes=9),
        )["settled"] >= 1
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_multi_quote_shadow WHERE asset='BTC' "
                "AND target_opened_at=?",(target.isoformat(),),
            )
            conn.execute(
                "DELETE FROM perp_multi_forward WHERE asset='BTC' "
                "AND target_opened_at=?",(target.isoformat(),),
            )
            conn.execute(
                "DELETE FROM perp_forward_audit WHERE asset='BTC' "
                "AND target_opened_at=? AND model_version=?",
                (target.isoformat(),BASE_MODEL),
            )
            for venue in ("hyperliquid","binance","okx"):
                conn.execute("DELETE FROM perp_feed_health WHERE venue=? AND asset='BTC'",
                             (venue,))
        store.close()


def test_stale_but_accepted_source_gets_less_influence(tmp_path):
    now = datetime(2026, 10, 30, 14, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "freshweights.sqlite")
    try:
        seed(store, now, venues=("hyperliquid", "binance", "okx"))
        # All sources are accepted, but the last two were observed earlier.
        with store.connect() as conn:
            conn.execute(
                """UPDATE perp_forward_audit SET issued_at=?
                   WHERE venue IN ('binance','okx')""",
                ((now-timedelta(minutes=2)).isoformat(),),
            )
        assert MultiExchangeResearch(store).issue(now=now) == 1
        with store.connect() as conn:
            row = conn.execute("SELECT contributors_json FROM perp_multi_forward").fetchone()
        contributions = {x["venue"]: x for x in json.loads(row["contributors_json"])}
        assert len(contributions) == 3
        assert contributions["hyperliquid"]["quality"] > contributions["okx"]["quality"]
        assert contributions["hyperliquid"]["quality"] > contributions["binance"]["quality"]
        assert contributions["hyperliquid"]["weight"] >= contributions["okx"]["weight"]
        assert sum(x["weight"] for x in contributions.values()) == pytest.approx(1.)
    finally:
        store.close()
