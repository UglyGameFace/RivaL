"""Proof-review scope selection is a pure function of the review stamp.

Slice 2 flip attempt 3 (2026-11-23): slot 1496220 agreed across bot,
worker, and canary, but slot 1496221 diverged -- the worker selected
(phemex, SOLUSDT, USDT, 15) while the bot's live row selected
(lighter, BTC, USD, 15), and a later canary recompute returned
NO_SCOPE. Root cause: ``ShadowLearningMonitor._eligible_streams``
filtered candles with ``first_observed_at >= stamp - 20min`` but took
``MAX(first_observed_at)`` unbounded, and read the round-robin's
last-evaluation ordering from ``perp_shadow_evaluations`` unbounded.
A candle landing -- or an evaluation saved -- after the stamp but
before the recompute therefore changed the eligible set for the same
logical slot: selection was a function of WHEN the query executed.

These tests pin the fix: both inputs are bounded above by the stamp,
so executing the selection at any later moment, after any amount of
post-stamp drift, yields the identical eligible list and proof scope.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import promotion_driver as driver
from parlay_bot.perpetuals.forward_audit import (
    family_proof_review_slot,
    select_family_proof_scope,
    select_live_streams,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.shadow_monitor import MODEL, ShadowLearningMonitor
from parlay_bot.storage.hot import SQLiteHotStore

BOUNDARY = datetime(2026, 11, 2, 16, 20, tzinfo=UTC)  # a 20-min boundary
LIGHTER = ("lighter", "BTC", "USD")       # the bot's slot-1496221 scope
PHEMEX = ("phemex", "SOLUSDT", "USDT")    # the worker's divergent scope
KUCOIN = ("kucoin", "XBTUSDTM", "USDT")


def _candle(store, stream, observed_at: datetime) -> None:
    PerpetualMarketStore(store).initialize()
    ShadowLearningMonitor(store).initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles (
              venue,contract,quote,interval_minutes,opened_at,closed_at,
              first_observed_at,open_price,high_price,low_price,close_price,
              volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT DO NOTHING""",
            (stream[0], stream[1], stream[2], 5,
             (observed_at - timedelta(minutes=5)).isoformat(),
             observed_at.isoformat(), observed_at.isoformat(),
             100.0, 101.0, 99.0, 100.5, 1.0, 1))


def _evaluation(store, stream, evaluated_at: datetime) -> None:
    ShadowLearningMonitor(store).initialize()
    asset = "BTC" if "BTC" in stream[1] or "XBT" in stream[1] else "SOL"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_shadow_evaluations (
              venue,contract,quote,asset,horizon_minutes,model_version,
              evaluated_at,observed_candles,causal_examples,evaluated,
              matured_updates,brier,baseline_brier,directional_accuracy,
              candidate_beats_baseline) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT (venue,contract,quote,horizon_minutes,model_version)
              DO UPDATE SET evaluated_at=excluded.evaluated_at""",
            (stream[0], stream[1], stream[2], asset, 5, MODEL,
             evaluated_at.isoformat(), 10, 0, 0, 0, None, None, None, 0))


def _scope_at(store, stamp):
    return driver.select_proof_scope(store, stamp)


def _find_divergent_stamp(store):
    """A slot stamp whose proof scope differs between the full
    {LIGHTER, PHEMEX} eligible set and the PHEMEX-only set -- the
    production divergence shape, so the fixture cannot pass vacuously."""
    for step in range(12):
        boundary = BOUNDARY + timedelta(minutes=20 * step)
        stamp = boundary + timedelta(
            seconds=driver.PROMOTION_STAMP_OFFSET_SECONDS)
        proof_slot = family_proof_review_slot(stamp)
        full = select_family_proof_scope(
            select_live_streams(
                [LIGHTER, PHEMEX], slot=int(stamp.timestamp() // 300)),
            hour_slot=proof_slot)
        reduced = select_family_proof_scope(
            select_live_streams(
                [PHEMEX], slot=int(stamp.timestamp() // 300)),
            hour_slot=proof_slot)
        if full is not None and full != reduced:
            return stamp, full
    raise AssertionError("no divergent stamp in 12 slots")


def test_selection_is_pure_in_the_stamp_despite_post_stamp_drift(tmp_path):
    store = SQLiteHotStore(tmp_path / "asof.sqlite")
    try:
        stamp, expected_scope = _find_divergent_stamp(store)
        boundary = stamp - timedelta(
            seconds=driver.PROMOTION_STAMP_OFFSET_SECONDS)
        # As of the stamp: both streams have a fresh candle, nothing else.
        _candle(store, LIGHTER, stamp - timedelta(seconds=120))
        _candle(store, PHEMEX, stamp - timedelta(seconds=60))
        _evaluation(store, LIGHTER, stamp - timedelta(hours=1))

        monitor = ShadowLearningMonitor(store)
        eligible_before = monitor._eligible_streams(now=stamp, limit=40)
        assert LIGHTER in eligible_before and PHEMEX in eligible_before
        slot_before, scope_before = _scope_at(store, stamp)
        assert scope_before == expected_scope

        # Drift that lands AFTER the stamp but BEFORE a later recompute
        # (the worker fires at +450 s; the canary later still):
        # a newer candle for LIGHTER (its MAX jumps past the stamp),
        # a first candle for a third stream, and a shadow evaluation
        # saved after the stamp for PHEMEX.
        _candle(store, LIGHTER, stamp + timedelta(seconds=180))
        _candle(store, KUCOIN, stamp + timedelta(seconds=240))
        _evaluation(store, PHEMEX, stamp + timedelta(seconds=600))

        eligible_after = monitor._eligible_streams(now=stamp, limit=40)
        slot_after, scope_after = _scope_at(store, stamp)
        assert eligible_after == eligible_before
        assert (slot_after, scope_after) == (slot_before, scope_before)

        # Even more candle drift, even later execution: still identical,
        # list order included.
        _candle(store, PHEMEX, stamp + timedelta(seconds=900))
        assert monitor._eligible_streams(now=stamp, limit=40) == \
            eligible_before
        assert _scope_at(store, stamp) == (slot_before, scope_before)

        # Documented residual: perp_shadow_evaluations is upsert-in-place,
        # so once a stream's pre-stamp evaluation row is overwritten by a
        # post-stamp evaluation, its as-of ordering value is unrecoverable
        # and the stream falls back to never-evaluated ordering. The
        # eligible SET -- and therefore the proof scope, which consumes
        # the set -- remains pure in the stamp; list order can differ in
        # that one case (it could matter only if >40 streams were valid
        # and the limit-40 truncation bound, which production's ~36
        # streams do not reach).
        _evaluation(store, LIGHTER, stamp + timedelta(seconds=1200))
        assert set(monitor._eligible_streams(now=stamp, limit=40)) == \
            set(eligible_before)
        assert _scope_at(store, stamp) == (slot_before, scope_before)
        assert boundary == stamp - timedelta(
            seconds=driver.PROMOTION_STAMP_OFFSET_SECONDS)
    finally:
        store.close()


def test_post_stamp_candle_alone_does_not_create_eligibility(tmp_path):
    """A stream whose only candle lands after the stamp was not
    eligible at the stamp, however late the selection executes."""
    store = SQLiteHotStore(tmp_path / "asof2.sqlite")
    try:
        stamp = BOUNDARY + timedelta(
            seconds=driver.PROMOTION_STAMP_OFFSET_SECONDS)
        _candle(store, KUCOIN, stamp + timedelta(seconds=30))
        monitor = ShadowLearningMonitor(store)
        assert monitor._eligible_streams(now=stamp, limit=40) == []
        assert _scope_at(store, stamp)[1] is None
    finally:
        store.close()
