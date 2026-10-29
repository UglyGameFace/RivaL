"""Fictional funded practice is private, capital-limited and restart-safe."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.live_quotes import BookQuote
from parlay_bot.perpetuals.paper_ledger import PaperLedger
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def _quote(*, stamp, bid=100.0, ask=100.1):
    return BookQuote(
        "BTC", "binance_usdm", "BTCUSDT",
        bid, 10, ask, 10, stamp, stamp,
    )


def test_fictional_funding_reserves_and_releases_realized_quote_costs(tmp_path):
    hot = SQLiteHotStore(tmp_path / "practice.sqlite")
    paper = PaperLedger(hot)
    stamp = datetime.now(UTC)
    try:
        initial = paper.fund("123", starting_usdt=100.0)
        assert initial["practice"]["funded"] == 100
        assert initial["practice"]["available"] == 100
        assert paper.fund("123", starting_usdt=200)["practice"]["funded"] == 200
        assert paper.fund("123")["practice"]["funded"] == 200
        paper.open(
            user_id="123", position_id="770001",
            quote=_quote(stamp=stamp - timedelta(seconds=3)),
            side="long", notional_usdt=125,
        )
        balance = PaperLedger(hot).summary("123")
        assert balance["practice"]["available"] == pytest.approx(75)
        assert balance["practice"]["committed"] == pytest.approx(125)
        with pytest.raises(ValueError, match="fictional available"):
            paper.open(
                user_id="123", position_id="770002",
                quote=_quote(stamp=stamp - timedelta(seconds=3)),
                side="long", notional_usdt=100,
            )
        with pytest.raises(ValueError, match="preserved"):
            paper.fund("123", starting_usdt=10000)
        closed = PaperLedger(hot).close(
            user_id="123", asset="BTC",
            quote=_quote(
                stamp=stamp - timedelta(seconds=1),
                bid=99.0, ask=99.1,
            ),
        )
        assert closed.assumed_net_usdt < 0
        summary = paper.summary("123")
        assert summary["practice"]["committed"] == 0
        assert summary["practice"]["equity"] == pytest.approx(
            200 + closed.assumed_net_usdt
        )
        assert summary["practice"]["available"] == pytest.approx(
            summary["practice"]["equity"]
        )
        assert summary["practice_feedback"]["losses"] == 1
        assert summary["practice_feedback"]["training_eligible"] is False
        assert paper.summary("456")["practice"]["equity"] == 10000
    finally:
        hot.close()


@pytest.mark.parametrize("balance", [0, -10, float("nan"), 100000.01])
def test_fake_balances_must_be_bounded(tmp_path, balance):
    hot = SQLiteHotStore(tmp_path / "invalid.sqlite")
    try:
        with pytest.raises(ValueError):
            PaperLedger(hot).fund("123", starting_usdt=balance)
    finally:
        hot.close()


def test_pg16_fictional_account_isolation_and_restart():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL integration service")
    hot = PostgresHotStore(dsn)
    paper = PaperLedger(hot)
    user = "987250125"
    try:
        result = paper.fund(user, starting_usdt=1200.0)
        assert result["practice"]["equity"] == 1200
        restarted = PaperLedger(PostgresHotStore(dsn))
        again = restarted.summary(user)
        assert again["practice"]["available"] == 1200
        assert restarted.summary("987250126")["practice"]["funded"] == 10000
        assert restarted.fund(user, starting_usdt=1200)["practice"]["funded"] == 1200
        assert restarted.fund(user)["practice"]["funded"] == 1200
    finally:
        with hot.connect() as conn:
            conn.execute(
                "DELETE FROM rival_practice_accounts WHERE user_id IN (?,?)",
                (user, "987250126"),
            )
        hot.close()
