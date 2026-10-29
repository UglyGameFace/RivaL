"""Quote-based simulated P&L is durable, private and never mislabeled as fills."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.live_quotes import BookQuote
from parlay_bot.perpetuals.paper_ledger import PaperLedger
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def quote(asset="BTC", *, when=None, bid=100, ask=100.1, liquidity=10):
    stamp = when or datetime.now(UTC)
    return BookQuote(
        asset, "binance_usdm", asset+"USDT", bid, liquidity,
        ask, liquidity, stamp, stamp,
    )


def test_paper_ledger_atomic_private_after_cost_and_restart(tmp_path):
    store = SQLiteHotStore(tmp_path / "paper.sqlite")
    ledger = PaperLedger(store)
    now = datetime.now(UTC)
    entry = quote(when=now - timedelta(seconds=3))
    exit_quote = quote(
        when=now - timedelta(seconds=1), bid=99.4, ask=99.5,
    )
    try:
        opened = ledger.open(
            user_id="123", position_id="9001", quote=entry,
            side="long", notional_usdt=25,
        )
        assert "Paper LONG" in opened
        assert "idempotent" in ledger.open(
            user_id="123", position_id="9001", quote=entry,
            side="long", notional_usdt=25,
        )
        with pytest.raises(ValueError, match="another user"):
            ledger.open(
                user_id="456", position_id="9001", quote=entry,
                side="long", notional_usdt=25,
            )
        with pytest.raises(ValueError, match="Close the existing"):
            ledger.open(
                user_id="123", position_id="9002", quote=entry,
                side="short", notional_usdt=25,
            )
        assert ledger.summary("123")["open"] == 1
        assert ledger.summary("456")["open"] == 0
        restarted = PaperLedger(store)
        closed = restarted.close(user_id="123", asset="BTC", quote=exit_quote)
        assert closed.gross_usdt < 0
        assert closed.assumed_cost_usdt > 0
        assert closed.assumed_net_usdt < closed.gross_usdt
        result = restarted.summary("123")
        assert result["open"] == 0 and result["closed"] == 1
        assert result["assumed_net_usdt"] == pytest.approx(closed.assumed_net_usdt)
        assert not result["actual_profit_verified"]
        with pytest.raises(ValueError, match="No open"):
            restarted.close(user_id="123", asset="BTC", quote=exit_quote)
    finally:
        store.close()


@pytest.mark.parametrize("bad", [
    0, -10, 251, float("nan"), float("inf"),
])
def test_paper_size_limit_prevents_risky_or_invalid_entries(tmp_path, bad):
    store = SQLiteHotStore(tmp_path / "limits.sqlite")
    ledger = PaperLedger(store)
    try:
        with pytest.raises(ValueError, match="between 0 and 250"):
            ledger.open(
                user_id="123", position_id="9010", quote=quote(),
                side="long", notional_usdt=bad,
            )
    finally:
        store.close()


def test_paper_short_assumed_pnl_and_top_book_rejection(tmp_path):
    store = SQLiteHotStore(tmp_path / "short.sqlite")
    ledger = PaperLedger(store)
    now = datetime.now(UTC)
    try:
        with pytest.raises(ValueError, match="liquidity"):
            ledger.open(
                user_id="123", position_id="12", side="long",
                notional_usdt=200,
                quote=quote(when=now, liquidity=0.1),
            )
        ledger.open(
            user_id="123", position_id="13", side="short",
            notional_usdt=25, quote=quote(when=now - timedelta(seconds=3)),
        )
        close = ledger.close(
            user_id="123", asset="BTC",
            quote=quote(when=now - timedelta(seconds=1), bid=92, ask=92.1),
        )
        assert close.gross_usdt > 0
        assert 0 < close.assumed_net_usdt < close.gross_usdt
    finally:
        store.close()


def test_paper_postgres16_persists_and_has_idempotent_schema():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration database unavailable")
    store = PostgresHotStore(dsn)
    ledger = PaperLedger(store)
    now = datetime.now(UTC)
    user = "981270000"
    try:
        ledger.initialize()
        ledger.initialize()
        ledger.open(
            user_id=user, position_id="981270001", quote=quote(when=now),
            side="long", notional_usdt=25,
        )
        restarted = PaperLedger(PostgresHotStore(dsn))
        assert restarted.summary(user)["open"] == 1
        closed = restarted.close(
            user_id=user, asset="BTC",
            quote=quote(when=now + timedelta(seconds=1)),
        )
        assert closed.assumed_net_usdt < 0
        assert restarted.summary(user)["closed"] >= 1
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM rival_paper_positions WHERE user_id=?", (user,),
            )
        store.close()
