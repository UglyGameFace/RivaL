"""Crash recovery, cross-source spike math, and strictly future-only outcomes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.analysis_evidence import build_evidence
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.spike_watch import SpikeWatch
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)
VENUES = (
    ("hyperliquid", "BTC", "USDC"),
    ("dydx", "BTC-USD", "USD"),
    ("gate", "BTC_USDT", "USDT"),
    ("okx", "BTC-USDT-SWAP", "USDT"),
)


def seed(market, *, now=NOW, venues=VENUES, surge=True, old_backfill=False,
         final_volume=3.5):
    market.initialize()
    for venue, contract, quote in venues:
        with market.store.connect() as conn:
            for index in range(27):
                opened = now - timedelta(minutes=5*(27-index))
                close = 100 + index*.01 + (2 if surge and index == 26 else 0)
                closed = opened+timedelta(minutes=5)-timedelta(milliseconds=1)
                observed = (now+timedelta(minutes=15)
                            if old_backfill else now)
                conn.execute(
                    """INSERT INTO perp_market_candles
                       (venue,contract,quote,interval_minutes,opened_at,closed_at,
                        first_observed_at,open_price,high_price,low_price,
                        close_price,volume_base,trades)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue,contract,quote,5,opened.isoformat(),closed.isoformat(),
                     observed.isoformat(),close,close+1,close-1,close,
                     final_volume if index == 26 else 1.,10),
                )
        market.record_feed_health(
            venue=venue, asset="BTC", status="ok",
            attempted_at=now,last_candle_close=now-timedelta(milliseconds=1),
            new_candles=27,total_candles=27,
        )


def next_candles(market, *, seen_at, venue_count=4, price=104.5):
    for venue, contract, quote in VENUES[:venue_count]:
        target = NOW
        with market.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,
                    close_price,volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue,contract,quote,5,target.isoformat(),
                 (target+timedelta(minutes=5)-timedelta(milliseconds=1)).isoformat(),
                 seen_at.isoformat(),price,price+1,price-1,price,1.,10),
            )


def watcher(tmp_path):
    store = SQLiteHotStore(tmp_path / "spike.sqlite")
    market = PerpetualMarketStore(store)
    monitor = SpikeWatch(market)
    monitor.initialize()
    return store, market, monitor


def test_source_aligned_spike_is_idempotent_and_recovers_after_restart(tmp_path):
    store, market, monitor = watcher(tmp_path)
    try:
        seed(market)
        names = tuple(v[0] for v in VENUES)
        first = monitor.scan(asset="BTC", venues=names, now=NOW)
        assert first["status"] == "watching"
        assert first["event"]["kind"] == "price_and_volume"
        assert first["event"]["source_count"] == 4
        assert first["event"]["direction"] == "up"
        assert monitor.scan(asset="BTC", venues=names, now=NOW)["event"]
        assert SpikeWatch(PerpetualMarketStore(store)).snapshot(
            asset="BTC", now=NOW,
        )["events_24h"] == 1
        market.research_spike_watcher = monitor
        evidence, digest = build_evidence(market,asset="BTC",venues=names,now=NOW)
        assert evidence["spike_analytics"]["events_24h"] == 1
        assert evidence["cold_archive_readback"] == "not_performed_by_live_research"
        assert digest
        next_candles(market,seen_at=NOW+timedelta(minutes=5))
        assert monitor.settle(asset="BTC",now=NOW+timedelta(minutes=6)) == 1
        assert monitor.settle(asset="BTC",now=NOW+timedelta(minutes=6)) == 0
        receipt = monitor.snapshot(asset="BTC",now=NOW+timedelta(minutes=6))
        assert receipt["scored_continuations_24h"] == 1
        assert receipt["scored_reversals_24h"] == 0
        assert receipt["model_edge_verified"] is False
    finally:
        store.close()


@pytest.mark.parametrize(("old_backfill","surge","expected"),[
    (True,True,"insufficient_fresh_sources"),
    (False,False,"watching"),
])
def test_old_first_seen_or_normal_moves_do_not_trigger_spike(
    tmp_path, old_backfill, surge, expected,
):
    store, market, monitor = watcher(tmp_path)
    try:
        seed(market,old_backfill=old_backfill,surge=surge,final_volume=1)
        result = monitor.scan(asset="BTC",venues=tuple(v[0] for v in VENUES),now=NOW)
        assert result["status"] == expected
        assert result["event"] is None
        assert monitor.snapshot(asset="BTC",now=NOW)["events_24h"] == 0
    finally:
        store.close()


def test_unsynchronized_source_windows_cannot_fake_cross_venue_confirmation(tmp_path):
    store, market, monitor = watcher(tmp_path)
    try:
        seed(market,venues=VENUES[:2])
        seed(market,venues=VENUES[2:],now=NOW-timedelta(minutes=5))
        result = monitor.scan(asset="BTC",venues=tuple(v[0] for v in VENUES),now=NOW)
        assert result["status"] == "insufficient_fresh_sources"
        assert result["event"] is None
    finally:
        store.close()


def test_historical_future_backfill_cannot_be_mistaken_for_forecast_success(tmp_path):
    store, market, monitor = watcher(tmp_path)
    try:
        seed(market)
        monitor.scan(asset="BTC",venues=tuple(v[0] for v in VENUES),now=NOW)
        # Future candle restored 3h late must NOT count as a live observation.
        next_candles(market,seen_at=NOW+timedelta(hours=3))
        assert monitor.settle(asset="BTC",now=NOW+timedelta(hours=3)) == 1
        outcome = monitor.snapshot(asset="BTC",now=NOW+timedelta(hours=3))
        assert outcome["unverified_24h"] == 1
        assert outcome["scored_continuations_24h"] == 0
        assert outcome["scored_reversals_24h"] == 0
    finally:
        store.close()


def test_missing_sources_never_get_automatic_correctness_credit(tmp_path):
    store, market, monitor = watcher(tmp_path)
    try:
        seed(market)
        monitor.scan(asset="BTC",venues=tuple(v[0] for v in VENUES),now=NOW)
        assert monitor.settle(asset="BTC",now=NOW+timedelta(minutes=6)) == 0
        assert monitor.settle(asset="BTC",now=NOW+timedelta(hours=3)) == 1
        assert monitor.snapshot(asset="BTC",now=NOW+timedelta(hours=3))[
            "unverified_24h"
        ] == 1
    finally:
        store.close()



def test_volume_only_spike_cannot_claim_directional_success(tmp_path):
    store, market, monitor = watcher(tmp_path)
    try:
        seed(market, surge=False, final_volume=3.5)
        event = monitor.scan(
            asset="BTC", venues=tuple(v[0] for v in VENUES), now=NOW,
        )["event"]
        assert event["kind"] == "volume"
        assert event["direction"] == "mixed"
        next_candles(market, seen_at=NOW+timedelta(minutes=5))
        monitor.settle(asset="BTC", now=NOW+timedelta(minutes=6))
        data = monitor.snapshot(asset="BTC", now=NOW+timedelta(minutes=6))
        assert data["scored_continuations_24h"] == 0
        assert data["scored_reversals_24h"] == 0
        assert data["recent"][0]["outcome"] == "mixed_no_direction"
    finally:
        store.close()
