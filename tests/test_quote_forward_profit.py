"""Only actual time-ordered quote snapshots can count as forward paper returns."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor, MODEL
from parlay_bot.perpetuals.live_quotes import BookQuote
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.profit_proof import (
    ProspectiveQuoteProfitMonitor,
    _daily_stress,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def _quote(asset, at, *, bid=100.0, ask=100.1, liquidity=100.0):
    return BookQuote(
        asset, "binance_usdm", asset + "USDT",
        bid, liquidity, ask, liquidity, at, at,
    )


def _forecast(store, *, asset, issued, horizon=5, probability=0.75,
              venue="gate", contract="BTC_USDT"):
    ref_open = issued - timedelta(minutes=5)
    # An issued five-minute target closes exactly five minutes after its
    # *future* target bar opens. Here it is a genuinely later outcome.
    target_open = issued + timedelta(minutes=horizon - 5)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,
                target_opened_at,probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, contract, "USDT", asset, horizon, MODEL,
                ref_open.isoformat(), 100.0, issued.isoformat(),
                target_open.isoformat(), probability, 0.5, 90,
            ),
        )


def test_immutable_quote_entry_then_exit_uses_real_bid_ask_not_candle(tmp_path):
    store = SQLiteHotStore(tmp_path / "quotes.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    proof = ProspectiveQuoteProfitMonitor(store)
    issued = datetime.now(UTC) - timedelta(minutes=5)
    entry = issued + timedelta(seconds=20)
    exit_at = issued + timedelta(minutes=5, seconds=30)
    try:
        _forecast(store, asset="BTC", issued=issued, probability=0.8)
        assert proof.observe(_quote("BTC", entry), now=entry)["opened"] == 1
        assert proof.observe(_quote("BTC", entry), now=entry)["opened"] == 0
        closing = proof.observe(
            _quote("BTC", exit_at, bid=101.5, ask=101.6), now=exit_at,
        )
        assert closing["closed"] == 1
        result = proof.proof(asset="BTC", horizon_minutes=5,
                             now=issued + timedelta(minutes=9))
        assert result.eligible_forecast_windows == 1
        assert result.verified_quote_exits == 1
        assert result.coverage == 1
        assert result.net_conservative_bps is not None
        assert result.net_conservative_bps > 0
        assert result.status == "NOT_PROVEN"
        assert not result.real_fills_verified
        assert not result.quote_shadow_qualified
        with store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_forward_quote_profit WHERE asset='BTC'"
            ).fetchone()
            assert row["state"] == "EXIT_QUOTED"
            assert row["entry_ask"] == 100.1
            assert row["exit_bid"] == 101.5
            assert row["side"] == "LONG"
    finally:
        store.close()


def test_shorts_use_bid_then_ask_and_losses_visible(tmp_path):
    store = SQLiteHotStore(tmp_path / "loss.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    proof = ProspectiveQuoteProfitMonitor(store)
    issued = datetime.now(UTC) - timedelta(minutes=5)
    try:
        _forecast(store, asset="BTC", issued=issued, probability=0.25)
        proof.observe(_quote("BTC", issued + timedelta(seconds=1)),
                      now=issued + timedelta(seconds=1))
        proof.observe(
            _quote("BTC", issued + timedelta(minutes=5), bid=105, ask=105.1),
            now=issued + timedelta(minutes=5),
        )
        result = proof.proof(asset="BTC", horizon_minutes=5,
                             now=issued + timedelta(minutes=9))
        assert result.verified_quote_exits == 1
        assert result.net_conservative_bps < 0
        assert result.status == "NOT_PROVEN"
    finally:
        store.close()


def test_delayed_exit_never_backfills_profitable_price(tmp_path):
    store = SQLiteHotStore(tmp_path / "missed.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    proof = ProspectiveQuoteProfitMonitor(store)
    issued = datetime.now(UTC) - timedelta(minutes=10)
    try:
        _forecast(store, asset="BTC", issued=issued, probability=0.9)
        proof.observe(
            _quote("BTC", issued + timedelta(seconds=10)),
            now=issued + timedelta(seconds=10),
        )
        late = issued + timedelta(minutes=10)
        assert proof.expire_unobserved(now=late) == 1
        changed = proof.observe(
            _quote("BTC", late, bid=200, ask=200.1), now=late,
        )
        assert changed["closed"] == 0
        result = proof.proof(asset="BTC", horizon_minutes=5, now=late)
        assert result.verified_quote_exits == 0
        assert result.missing_exits == 1
        assert result.coverage == 0
        assert result.status == "NOT_PROVEN"
    finally:
        store.close()


def test_no_forecast_no_fabricated_shadows_or_money(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    profit = ProspectiveQuoteProfitMonitor(store)
    now = datetime.now(UTC)
    try:
        assert profit.observe(_quote("BTC", now), now=now)["opened"] == 0
        result = profit.proof(asset="BTC", horizon_minutes=15, now=now)
        assert result.eligible_forecast_windows == 0
        assert result.net_severe_bps is None
        assert not result.real_fills_verified
        assert result.status == "NOT_PROVEN"
    finally:
        store.close()


def test_timestamp_quote_validation_and_no_lookahead(tmp_path):
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    profit = ProspectiveQuoteProfitMonitor(store)
    issued = datetime.now(UTC)
    try:
        _forecast(store, asset="BTC", issued=issued)
        with pytest.raises(Exception, match="stale"):
            profit.observe(_quote("BTC", issued - timedelta(minutes=2)), now=issued)
        assert profit.observe(
            _quote("BTC", issued - timedelta(seconds=1)),
            now=issued - timedelta(seconds=1),
        )["opened"] == 0
        assert profit.observe(_quote("BTC", issued), now=issued)["opened"] == 1
        with pytest.raises(ValueError):
            profit.proof(asset="DOGE", horizon_minutes=5, now=issued)
    finally:
        store.close()


def test_day_stress_with_one_day_cannot_claim_confidence():
    mean, lower, winning = _daily_stress([
        ("2026-10-29", 40.0), ("2026-10-29", 80.0),
    ])
    assert mean == 60
    assert lower is None
    assert winning == 1
    assert _daily_stress([]) == (None, None, 0)


def test_postgres_forward_quote_ledger_restart_and_idempotence():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres integration test database not configured")
    store = PostgresHotStore(dsn)
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    profit = ProspectiveQuoteProfitMonitor(store)
    issued = datetime.now(UTC) - timedelta(minutes=5)
    try:
        profit.initialize()
        profit.initialize()
        _forecast(
            store, asset="BTC", issued=issued, probability=0.8,
            venue="gate", contract="BTC_USDT",
        )
        entry = issued + timedelta(seconds=2)
        assert profit.observe(_quote("BTC", entry), now=entry)["opened"] == 1
        restarted = ProspectiveQuoteProfitMonitor(PostgresHotStore(dsn))
        assert restarted.observe(_quote("BTC", entry), now=entry)["opened"] == 0
        closed_at = issued + timedelta(minutes=5, seconds=2)
        assert restarted.observe(
            _quote("BTC", closed_at, bid=102, ask=102.1), now=closed_at,
        )["closed"] == 1
        assert restarted.proof(
            asset="BTC", horizon_minutes=5,
            now=issued + timedelta(minutes=7),
        ).verified_quote_exits >= 1
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM rival_forward_quote_profit WHERE asset='BTC' "
                "AND target_close_at=?",
                ((issued + timedelta(minutes=5)).isoformat(),),
            )
            conn.execute(
                "DELETE FROM perp_forward_audit WHERE venue='gate' "
                "AND contract='BTC_USDT' AND reference_opened_at=?",
                ((issued - timedelta(minutes=5)).isoformat(),),
            )
        store.close()
