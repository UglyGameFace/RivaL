"""Read-only production audit counts agree with the persisted source ledgers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.research.database_integrity import (
    perpetual_evidence_backlog,
    sports_settlement_backlog,
)
from parlay_bot.research.sports_results import SportsResultStore
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_settlement_ties_and_reconciliation import (
    START,
    insert_candle,
    insert_forecast,
)
from tests.test_sports_settlement_recovery import event, insert_pending_forecast


def test_database_integrity_reads_only_actual_backlog_and_no_trade_quotes(tmp_path):
    store = SQLiteHotStore(tmp_path / "integrity.sqlite")
    now = START + timedelta(days=4)
    target = START + timedelta(hours=1)
    issued = target - timedelta(minutes=30)
    try:
        store.initialize()
        sports = SportsResultStore(store)
        sports.save_finalized(
            event(), observed_at=datetime(2026, 10, 20, 14, tzinfo=UTC),
        )
        insert_pending_forecast(store)
        assert sports_settlement_backlog(store) == 1

        market = PerpetualMarketStore(store)
        ForwardAccuracyMonitor(market).initialize()
        MultiExchangeResearch(store).initialize()
        with store.connect() as conn:
            insert_candle(
                conn, opened=target, close=101.0,
                observed=target + timedelta(minutes=6),
            )
            insert_forecast(
                conn, target=target, issued=issued, reference_close=100.0,
            )
            conn.execute(
                """INSERT INTO perp_multi_forward
                   (asset,horizon_minutes,target_opened_at,issued_at,
                    model_version,probability_up,reference_probability_up,
                    reference_venue,reference_contract,reference_quote,
                    reference_opened_at,contributor_count,contributors_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "BTC", 5, target.isoformat(), issued.isoformat(),
                    MULTI_MODEL, .51, .49, "hyperliquid", "BTC", "USDC",
                    (target - timedelta(minutes=5)).isoformat(), 3, "[]",
                ),
            )
            conn.execute(
                """INSERT INTO perp_multi_quote_shadow
                   (asset,horizon_minutes,target_opened_at,venue,entry_at,
                    entry_bid,entry_ask,state,challenger_gross_bps,
                    incumbent_gross_bps)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "BTC", 5, target.isoformat(), "hyperliquid",
                    issued.isoformat(), 100.0, 101.0, "EXIT_QUOTED", 0.0, 0.0,
                ),
            )
        assert perpetual_evidence_backlog(store, now=now) == {
            "overdue_forecasts": 1,
            "recoverable_overdue": 1,
            "closed_quote_windows": 1,
            "neither_traded_quote_windows": 1,
        }
        # The monitoring method cannot itself settle or rewrite history.
        with store.connect() as conn:
            pending = conn.execute(
                "SELECT settled_at FROM perp_forward_audit"
            ).fetchone()
        assert pending["settled_at"] is None
        assert sports.reconcile_unsettled() == [("SOCCER", "EPL")]
        assert sports_settlement_backlog(store) == 0
    finally:
        store.close()


def test_database_integrity_rejects_naive_audit_clock(tmp_path):
    store = SQLiteHotStore(tmp_path / "naive.sqlite")
    try:
        with pytest.raises(ValueError, match="timezone-aware"):
            perpetual_evidence_backlog(store, now=datetime(2026, 10, 24))
    finally:
        store.close()
