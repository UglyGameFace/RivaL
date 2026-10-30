"""Promotion closes the loop: a challenger earns the live path, or not.

Qualification needs BOTH paired proofs: the governance Brier screens on
shared target windows (100 targets, 7 days, minimum gain, consistent
days, positive stress bound) and a paired after-cost quote proof: at
least 20 real challenger trades with a positive paired after-cost
improvement. Cash-vs-cash windows prove nothing and never count.
Anything else holds or demotes, and a stale promotion falls back to the
incumbent live model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.perpetuals.promotion import (
    evaluate_promotion,
    multi_summary,
    promoted_engine,
)
from parlay_bot.storage.hot import SQLiteHotStore


def seed_multi_settled(
    store,
    now,
    *,
    count,
    days_span=6,
    challenger=0.20,
    incumbent=0.25,
    base_offset_days=6,
    step_minutes=None,
    start_index=0,
    probability=.7,
    reference=.6,
):
    """Settled fused rows across calendar days, one per target window."""
    MultiExchangeResearch(store).initialize()
    base = now - timedelta(days=base_offset_days)
    if step_minutes is None:
        step = timedelta(minutes=(days_span * 24 * 60) // max(count, 1))
    else:
        step = timedelta(minutes=step_minutes)
    with store.connect() as conn:
        for i in range(count):
            target = base + step * (start_index + i)
            issued = target - timedelta(minutes=10)
            conn.execute(
                """INSERT INTO perp_multi_forward (
                   asset,horizon_minutes,target_opened_at,issued_at,
                   model_version,probability_up,reference_probability_up,
                   reference_venue,reference_contract,reference_quote,
                   reference_opened_at,contributor_count,contributors_json,
                   settled_at,outcome_up,challenger_brier,incumbent_brier
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "BTC",
                    5,
                    target.isoformat(),
                    issued.isoformat(),
                    MULTI_MODEL,
                    probability,
                    reference,
                    "hyperliquid",
                    "BTC",
                    "USDC",
                    (target - timedelta(minutes=5)).isoformat(),
                    4,
                    "[]",
                    (target + timedelta(minutes=10)).isoformat(),
                    1,
                    challenger,
                    incumbent,
                ),
            )


def seed_quote_closes(store, now, *, count, challenger_gross=10.0, incumbent_gross=5.0):
    """Paired EXIT_QUOTED windows for the first `count` settled targets."""
    with store.connect() as conn:
        targets = conn.execute(
            """SELECT target_opened_at FROM perp_multi_forward
               WHERE asset='BTC' AND horizon_minutes=5
               ORDER BY target_opened_at LIMIT ?""",
            (count,),
        ).fetchall()
        for row in targets:
            target = row["target_opened_at"]
            conn.execute(
                """INSERT INTO perp_multi_quote_shadow (
                   asset,horizon_minutes,target_opened_at,venue,
                   entry_at,entry_bid,entry_ask,state,
                   exit_at,exit_bid,exit_ask,
                   challenger_gross_bps,incumbent_gross_bps
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "BTC",
                    5,
                    target,
                    "hyperliquid",
                    target,
                    81000,
                    81001,
                    "EXIT_QUOTED",
                    target,
                    81010,
                    81011,
                    challenger_gross,
                    incumbent_gross,
                ),
            )


def promote(store, now, **kwargs):
    seed_multi_settled(store, now, count=100, **kwargs)
    seed_quote_closes(store, now, count=20)
    return evaluate_promotion(store, asset="BTC", horizon=5, now=now)


def test_qualified_challenger_with_quote_proof_promotes(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "promo.sqlite")
    try:
        row = promote(store, now)
        assert row["status"] == "promoted"
        assert row["engine"] == "multi"
        assert row["unique_target_windows"] == 100
        assert promoted_engine(store, asset="BTC", horizon=5, now=now) == "multi"
    finally:
        store.close()


def test_trailing_challenger_holds(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "hold.sqlite")
    try:
        row = promote(store, now, challenger=0.30)
        assert row["status"] == "held"
        assert promoted_engine(store, asset="BTC", horizon=5, now=now) == "live"
    finally:
        store.close()


def test_insufficient_targets_hold(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "thin.sqlite")
    try:
        seed_multi_settled(store, now, count=50)
        seed_quote_closes(store, now, count=20)
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["status"] == "held"
        assert "100" in row["reason"]
    finally:
        store.close()


def test_qualified_brier_but_thin_quotes_hold(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "thinquote.sqlite")
    try:
        seed_multi_settled(store, now, count=100)
        seed_quote_closes(store, now, count=5)
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["status"] == "held"
        assert "quote" in row["reason"]
        assert promoted_engine(store, asset="BTC", horizon=5, now=now) == "live"
    finally:
        store.close()


def test_promotion_demotes_when_evidence_collapses(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "demote.sqlite")
    try:
        assert promote(store, now)["status"] == "promoted"
        later = now + timedelta(days=8)
        seed_multi_settled(
            store,
            later,
            count=100,
            challenger=0.30,
            base_offset_days=0,
        )
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=later)
        assert row["status"] == "demoted"
        assert promoted_engine(store, asset="BTC", horizon=5, now=later) == "live"
    finally:
        store.close()


def test_no_trade_windows_cannot_promote(tmp_path):
    """Twenty cash-vs-cash windows prove no executable skill (P0)."""
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "notrade.sqlite")
    try:
        seed_multi_settled(
            store, now, count=100, probability=.5, reference=.5,
        )
        seed_quote_closes(
            store, now, count=20, challenger_gross=0.0, incumbent_gross=0.0,
        )
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["status"] == "held"
        assert row["challenger_trades"] == 0
        assert "trades" in row["reason"]
        assert promoted_engine(store, asset="BTC", horizon=5, now=now) == "live"
    finally:
        store.close()


def test_challenger_losing_after_costs_holds(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "loses.sqlite")
    try:
        seed_multi_settled(store, now, count=100)
        seed_quote_closes(
            store, now, count=20, challenger_gross=10.0, incumbent_gross=12.0,
        )
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["status"] == "held"
        assert row["challenger_net_bps"] == pytest.approx(-4.0)
        assert row["incumbent_net_bps"] == pytest.approx(-2.0)
    finally:
        store.close()


def test_challenger_trading_at_loss_does_not_beat_cash(tmp_path):
    """A challenger that trades at a loss does not beat a cash incumbent."""
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "cash.sqlite")
    try:
        seed_multi_settled(
            store, now, count=100, probability=.7, reference=.5,
        )
        seed_quote_closes(
            store, now, count=20, challenger_gross=10.0, incumbent_gross=0.0,
        )
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["status"] == "held"
        assert row["incumbent_trades"] == 0
    finally:
        store.close()


def test_stale_promotion_falls_back_to_live(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    try:
        assert promote(store, now)["status"] == "promoted"
        assert (
            promoted_engine(
                store,
                asset="BTC",
                horizon=5,
                now=now + timedelta(hours=49),
            )
            == "live"
        )
    finally:
        store.close()


def test_multi_summary_needs_fifty_settled(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "summary.sqlite")
    try:
        seed_multi_settled(
            store,
            now,
            count=49,
            days_span=1,
            base_offset_days=1,
            step_minutes=29,
        )
        assert (
            multi_summary(
                store,
                asset="BTC",
                horizon=5,
                now=now,
            )["model_beats_baseline"]
            is None
        )
        seed_multi_settled(
            store,
            now,
            count=1,
            days_span=1,
            base_offset_days=1,
            step_minutes=29,
            start_index=49,
        )
        assert (
            multi_summary(
                store,
                asset="BTC",
                horizon=5,
                now=now,
            )["model_beats_baseline"]
            is True
        )
    finally:
        store.close()
