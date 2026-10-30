"""Legacy profit ledger must price a forecast only with its own venue's book.

A Binance quote previously opened a shadow position off the chronologically
first forecast for the target, whatever its venue. The entry query now
requires venue='binance' AND contract=quote.symbol; the close path was
already safe (one row per target by primary key, plus a quote-identity
skip check). A Bitget forecast must never become a Binance-priced return.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import BookQuote
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.profit_proof import ProspectiveQuoteProfitMonitor
from parlay_bot.storage.hot import SQLiteHotStore


def seed_forecast(market, now, *, venue, contract, issued_ago, probability):
    ForwardAccuracyMonitor(market).initialize()
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, contract, "USDT", "BTC", 5, MODEL,
                (now - timedelta(minutes=10)).isoformat(), 81001,
                (now - issued_ago).isoformat(),
                (now + timedelta(minutes=4)).isoformat(),
                probability, .5, 120,
            ),
        )


def binance_quote(now):
    return BookQuote(
        asset="BTC", venue="binance_usdm", symbol="BTCUSDT",
        bid=81000, ask=81001, bid_quantity=10, ask_quantity=10,
        exchange_at=now, received_at=now,
    )


def profit_rows(store):
    with store.connect() as conn:
        return conn.execute(
            "SELECT forecast_venue, forecast_contract, state "
            "FROM rival_forward_quote_profit"
        ).fetchall()


@pytest.mark.asyncio
async def test_bitget_forecast_never_opens_a_binance_priced_position(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "mismatch.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_forecast(market, now, venue="bitget", contract="BTCUSDT",
                      issued_ago=timedelta(seconds=60), probability=.62)
        monitor = ProspectiveQuoteProfitMonitor(store)
        result = monitor.observe(binance_quote(now), now=now)
        assert result["opened"] == 0
        assert profit_rows(store) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_binance_forecast_opens_and_closes_with_its_own_book(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "matched.sqlite")
    market = PerpetualMarketStore(store)
    try:
        # Bitget is earlier, so without the venue filter it would win the
        # target slot; with the filter only the Binance forecast opens.
        seed_forecast(market, now, venue="bitget", contract="BTCUSDT",
                      issued_ago=timedelta(seconds=60), probability=.62)
        seed_forecast(market, now, venue="binance", contract="BTCUSDT",
                      issued_ago=timedelta(seconds=30), probability=.62)
        monitor = ProspectiveQuoteProfitMonitor(store)
        result = monitor.observe(binance_quote(now), now=now)
        assert result["opened"] == 1
        rows = profit_rows(store)
        assert len(rows) == 1
        assert rows[0]["forecast_venue"] == "binance"
        assert rows[0]["forecast_contract"] == "BTCUSDT"
        assert rows[0]["state"] == "OPEN"

        # A fresh instance (a restart) still sees the open position, and
        # a later Binance quote near the target closes exactly that row.
        restarted = ProspectiveQuoteProfitMonitor(store)
        assert len(profit_rows(store)) == 1
        close_at = now + timedelta(minutes=9)
        closed = restarted.observe(
            binance_quote(close_at), now=close_at,
        )
        assert closed["closed"] == 1
        assert profit_rows(store)[0]["state"] == "EXIT_QUOTED"
    finally:
        store.close()
