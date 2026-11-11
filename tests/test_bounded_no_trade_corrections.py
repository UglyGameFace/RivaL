"""The no-trade corrections check recent rows each cycle, the whole ledger daily.

They were unbounded UPDATEs over the main-model, test-model and fused ledgers
on every cycle or start, although new rows are graded correctly at
settlement. A recent withdrawal must still land at once, and an old row must
still be swept up within a day.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.storage import maintenance
from parlay_bot.storage.hot import SQLiteHotStore

MODEL = "prospective-logistic-persistent-v3"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


# --- The window --------------------------------------------------------------

def test_the_window_is_a_full_sweep_first_then_recent_until_a_day_passes(tmp_path):
    store = SQLiteHotStore(tmp_path / "window.sqlite")
    store.initialize()
    try:
        maintenance.initialize(store)
        with store.connect() as conn:
            assert maintenance.correction_cutoff(conn, "x", NOW) is None
            maintenance.record_full_sweep(conn, "x", NOW)
            later = NOW + timedelta(hours=23)
            assert maintenance.correction_cutoff(conn, "x", later) == (
                later - maintenance.RECENT).isoformat()
            # Each correction keeps its own record.
            assert maintenance.correction_cutoff(conn, "y", later) is None
            # A day later, the full sweep is due again.
            assert maintenance.correction_cutoff(
                conn, "x", NOW + maintenance.FULL_EVERY) is None
            # A sweep recorded in the future (bad clock) never suppresses it.
            maintenance.record_full_sweep(conn, "x", NOW + timedelta(hours=5))
            assert maintenance.correction_cutoff(conn, "x", NOW) is None
    finally:
        store.close()


def test_the_window_needs_a_utc_clock(tmp_path):
    store = SQLiteHotStore(tmp_path / "clock.sqlite")
    store.initialize()
    try:
        maintenance.initialize(store)
        with store.connect() as conn, pytest.raises(ValueError):
            maintenance.correction_cutoff(conn, "x", datetime(2026, 11, 10))
    finally:
        store.close()


# --- The three ledgers -------------------------------------------------------

def _store(tmp_path, name):
    store = SQLiteHotStore(tmp_path / name)
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    ShadowModelCompetition(store).initialize()
    return store, market


def _main_row(conn, *, venue, target, settled, outcome, trades):
    """A main-model result graded flat (close == reference) at ``settled``."""
    ref = target - timedelta(minutes=5)
    conn.execute(
        """INSERT INTO perp_market_candles (venue,contract,quote,interval_minutes,
             opened_at,closed_at,first_observed_at,open_price,high_price,low_price,
             close_price,volume_base,trades)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (venue, "BTC", "USDT", 5, target.isoformat(),
         (target + timedelta(minutes=5, milliseconds=-1)).isoformat(),
         (target + timedelta(minutes=5, seconds=10)).isoformat(),
         100., 100., 100., 100., 0., trades))
    conn.execute(
        """INSERT INTO perp_forward_audit (venue,contract,quote,asset,
             horizon_minutes,model_version,reference_opened_at,reference_close,
             issued_at,target_opened_at,probability_up,baseline_up,
             training_evaluated,settled_at,outcome_up,realized_close,
             model_brier,baseline_brier)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (venue, "BTC", "USDT", "BTC", 5, MODEL, ref.isoformat(), 100.,
         (ref + timedelta(minutes=1)).isoformat(), target.isoformat(), .6, .5, 100,
         settled.isoformat(), outcome, 100.,
         None if outcome is None else .36, None if outcome is None else .25))


def _outcomes(store, table="perp_forward_audit"):
    with store.connect() as conn:
        return {row["venue"]: row["outcome_up"] for row in conn.execute(
            f"SELECT venue,outcome_up FROM {table}")}


def test_main_ledger_recent_row_is_withdrawn_old_row_waits_for_the_daily_sweep(tmp_path):
    store, market = _store(tmp_path, "main.sqlite")
    try:
        with store.connect() as conn:
            # Both flat zero-trade results still carry a fake NOT-UP label.
            _main_row(conn, venue="okx", target=NOW - timedelta(hours=3),
                      settled=NOW - timedelta(hours=3), outcome=0, trades=0)
            _main_row(conn, venue="gate", target=NOW - timedelta(days=5),
                      settled=NOW - timedelta(days=5), outcome=0, trades=0)
            # A flat close that did trade is a genuine NOT-UP: never touched.
            _main_row(conn, venue="kucoin", target=NOW - timedelta(hours=2),
                      settled=NOW - timedelta(hours=2), outcome=0, trades=7)
        monitor = ForwardAccuracyMonitor(market)
        with store.connect() as conn:  # the daily sweep ran an hour ago
            maintenance.initialize(store)
            maintenance.record_full_sweep(
                conn, "forward_audit.no_trade", NOW - timedelta(hours=1))
        monitor._withdraw_no_trade_labels(NOW)
        assert _outcomes(store) == {"okx": None, "gate": 0, "kucoin": 0}
        # When the daily sweep comes due, the old row is corrected too.
        monitor._withdraw_no_trade_labels(NOW + timedelta(hours=22))
        assert _outcomes(store) == {"okx": None, "gate": 0, "kucoin": 0}
        monitor._withdraw_no_trade_labels(NOW + maintenance.FULL_EVERY)
        assert _outcomes(store) == {"okx": None, "gate": None, "kucoin": 0}
    finally:
        store.close()


def test_a_first_run_sweeps_everything_and_records_it(tmp_path):
    store, market = _store(tmp_path, "first.sqlite")
    try:
        with store.connect() as conn:
            _main_row(conn, venue="gate", target=NOW - timedelta(days=5),
                      settled=NOW - timedelta(days=5), outcome=0, trades=0)
        ForwardAccuracyMonitor(market)._withdraw_no_trade_labels(NOW)
        assert _outcomes(store) == {"gate": None}
        with store.connect() as conn:
            assert maintenance.correction_cutoff(
                conn, "forward_audit.no_trade", NOW + timedelta(minutes=5)) is not None
    finally:
        store.close()


def _shadow_row(conn, *, venue, target, settled, outcome):
    ref = target - timedelta(minutes=5)
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
             horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
             probability_up,incumbent_up,baseline_up,training_count,
             training_sha256,settled_at,outcome_up,model_brier,incumbent_brier,
             baseline_brier)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (venue, "BTC", "USDT", "BTC", 5, "knn21_v1", ref.isoformat(),
         (ref + timedelta(minutes=1)).isoformat(), target.isoformat(),
         .55, .6, .5, 300, "a" * 64, settled.isoformat(), outcome, .3, .36, .25))


def test_shadow_ledger_follows_the_same_window(tmp_path):
    store, _ = _store(tmp_path, "shadow.sqlite")
    try:
        with store.connect() as conn:
            for venue, age in (("okx", timedelta(hours=3)), ("gate", timedelta(days=5))):
                # The main-model result was withdrawn; the copied label was not.
                _main_row(conn, venue=venue, target=NOW - age, settled=NOW - age,
                          outcome=None, trades=0)
                _shadow_row(conn, venue=venue, target=NOW - age, settled=NOW - age,
                            outcome=0)
        competition = ShadowModelCompetition(store)
        with store.connect() as conn:
            maintenance.initialize(store)
            maintenance.record_full_sweep(conn, "shadow.no_trade", NOW - timedelta(hours=1))
        competition._withdraw_no_trade_labels(NOW)
        assert _outcomes(store, "rival_model_shadow_forecasts") == {"okx": None, "gate": 0}
        competition._withdraw_no_trade_labels(NOW + maintenance.FULL_EVERY)
        assert _outcomes(store, "rival_model_shadow_forecasts") == {"okx": None, "gate": None}
    finally:
        store.close()


def test_fused_ledger_start_sweeps_once_a_day_not_every_start(tmp_path, monkeypatch):
    store, _ = _store(tmp_path, "multi.sqlite")
    try:
        research = MultiExchangeResearch(store)
        research.initialize()
        calls = []
        original = maintenance.record_full_sweep

        def counted(conn, name, now=None):
            calls.append(name)
            original(conn, name, now)

        monkeypatch.setattr(maintenance, "record_full_sweep", counted)
        for _ in range(3):  # three more process starts
            research._withdraw_no_trade_labels()
        assert calls == []  # the first start already swept today
    finally:
        store.close()
