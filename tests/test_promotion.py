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


def seed_strict_21_day_paired_proof(store, now):
    """336 genuinely distinct target windows, 21 stable UTC days.

    Exactly 75% UP labels make the .75 challenger calibrated. The .50
    incumbent is the weaker paired baseline on the SAME target events.
    Source-aligned quote rows are paper-only and include severe-cost margin.
    """
    MultiExchangeResearch(store).initialize()
    start = now - timedelta(days=21)
    with store.connect() as conn:
        for day in range(21):
            for slot in range(16):
                target = start + timedelta(days=day, minutes=5 * slot)
                issued = target - timedelta(minutes=6)
                label = int(slot % 4 != 0)
                challenger = .75
                baseline = .50
                conn.execute(
                    """INSERT INTO perp_multi_forward (
                        asset,horizon_minutes,target_opened_at,issued_at,
                        model_version,probability_up,reference_probability_up,
                        reference_venue,reference_contract,reference_quote,
                        reference_opened_at,contributor_count,contributors_json,
                        settled_at,outcome_up,challenger_brier,incumbent_brier)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("BTC", 5, target.isoformat(), issued.isoformat(),
                     MULTI_MODEL, challenger, baseline, "hyperliquid",
                     "BTC", "USDC",
                     (target - timedelta(minutes=5)).isoformat(), 4, "[]",
                     (target + timedelta(minutes=6)).isoformat(),
                     label, (challenger-label)**2, (baseline-label)**2),
                )
                conn.execute(
                    """INSERT INTO perp_multi_quote_shadow (
                       asset,horizon_minutes,target_opened_at,venue,
                       entry_at,entry_bid,entry_ask,state,
                       exit_at,exit_bid,exit_ask,
                       challenger_gross_bps,incumbent_gross_bps)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("BTC", 5, target.isoformat(), "hyperliquid",
                     issued.isoformat(), 100, 101, "EXIT_QUOTED",
                     (target + timedelta(minutes=6)).isoformat(),
                     102, 103, 120., 30.),
                )


def test_qualified_challenger_with_quote_proof_promotes(tmp_path):
    # Keep synthetic 5-minute windows inside complete UTC days. Near
    # midnight a one-sample partial UTC day can falsely fail day-block stress.
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "promo.sqlite")
    try:
        seed_strict_21_day_paired_proof(store, now)
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["status"] == "promoted"
        assert row["engine"] == "multi"
        assert row["unique_target_windows"] >= 100
        assert promoted_engine(store, asset="BTC", horizon=5, now=now) == "multi"
    finally:
        store.close()


def test_trailing_challenger_holds(tmp_path):
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "hold.sqlite")
    try:
        row = promote(store, now, challenger=0.30)
        assert row["status"] == "held"
        assert promoted_engine(store, asset="BTC", horizon=5, now=now) == "live"
    finally:
        store.close()


def test_insufficient_targets_hold(tmp_path):
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
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
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
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
    # Keep synthetic 5-minute windows inside complete UTC days. Near
    # midnight a one-sample partial UTC day can falsely fail day-block stress.
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "demote.sqlite")
    try:
        seed_strict_21_day_paired_proof(store, now)
        assert evaluate_promotion(
            store, asset="BTC", horizon=5, now=now,
        )["status"] == "promoted"
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
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
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
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
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
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
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
    # Keep synthetic 5-minute windows inside complete UTC days. Near
    # midnight a one-sample partial UTC day can falsely fail day-block stress.
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    try:
        seed_strict_21_day_paired_proof(store, now)
        assert evaluate_promotion(
            store, asset="BTC", horizon=5, now=now,
        )["status"] == "promoted"
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
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
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


def test_unsettled_quote_windows_are_not_used_for_promotion(tmp_path):
    # Historical quote rows without verified finalized outcomes must never
    # inflate the short-horizon trade sample used for automatic promotion.
    now = datetime(2026, 10, 30, 14, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "unsettled-quote.sqlite")
    try:
        seed_strict_21_day_paired_proof(store, now)
        with store.connect() as conn:
            target = conn.execute(
                "SELECT target_opened_at FROM perp_multi_forward ORDER BY target_opened_at LIMIT 1"
            ).fetchone()["target_opened_at"]
            conn.execute(
                "UPDATE perp_multi_forward SET settled_at=NULL WHERE target_opened_at=?",
                (target,),
            )
        row = evaluate_promotion(store, asset="BTC", horizon=5, now=now)
        assert row["quote_windows"] == 335
        assert row["challenger_trades"] == 335
    finally:
        store.close()
