"""Slice 2 proof-review driver: slot replication, shadow proof, bot gate.

The worker-side driver must be the bot cycle's driver, moved: same slot,
same scope inputs, same evaluate() call, pinned to the slot boundary.
These tests pin that equivalence -- including the core proof that a
shadow (snapshot-isolated) evaluation and a live evaluation of the same
slot produce a byte-identical promotion row -- and the guard that a
shadow run which *selects* a family is never rolled back.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals import promotion_driver as driver
from parlay_bot.perpetuals.forward_audit import (
    ForwardAccuracyMonitor,
    family_proof_review_slot,
    select_family_proof_scope,
    select_live_streams,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_family_cost_promotion import _seed

NOW = datetime(2026, 11, 2, 16, 20, tzinfo=UTC)  # a 20-minute boundary
STREAM = ("hyperliquid", "BTC", "USDC")


def _seed_candle(store, *, observed_at: datetime) -> None:
    PerpetualMarketStore(store).initialize()
    ShadowLearningMonitor(store).initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles (
              venue,contract,quote,interval_minutes,opened_at,closed_at,
              first_observed_at,open_price,high_price,low_price,close_price,
              volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (STREAM[0], STREAM[1], STREAM[2], 5,
             (observed_at - timedelta(minutes=5)).isoformat(),
             observed_at.isoformat(), observed_at.isoformat(),
             100.0, 101.0, 99.0, 100.5, 1.0, 1))


def _scope_row(store, scope):
    venue, contract, quote, horizon = scope
    return driver.read_promotion_row(
        store, venue=venue, contract=contract, quote=quote, horizon=horizon)


def _bot_scope(store, stamp):
    """The bot cycle's own three calls, spelled out independently."""
    eligible = ShadowLearningMonitor(store)._eligible_streams(
        now=stamp, limit=40)
    selection = select_live_streams(
        eligible, slot=int(stamp.timestamp() // 300))
    slot = family_proof_review_slot(stamp)
    return slot, select_family_proof_scope(selection, hour_slot=slot)


def _horizon5_stamp() -> datetime:
    """First boundary at/after NOW whose rotation selects horizon 5."""
    for step in range(3):
        stamp = NOW + timedelta(minutes=20 * step)
        slot = family_proof_review_slot(stamp)
        scope = select_family_proof_scope([STREAM], hour_slot=slot)
        if scope is not None and scope[3] == 5:
            return stamp
    raise AssertionError("rotation never selects horizon 5")


def test_worker_driven_defaults_off_and_honors_env(monkeypatch):
    monkeypatch.delenv("RIVAL_PROMOTION_WORKER_DRIVEN", raising=False)
    driver.reset_worker_driven_cache()
    assert driver.worker_driven() is False
    monkeypatch.setenv("RIVAL_PROMOTION_WORKER_DRIVEN", "true")
    driver.reset_worker_driven_cache()
    assert driver.worker_driven() is True
    driver.reset_worker_driven_cache()


def test_slot_math_matches_bot_slot_function():
    for slot in range(driver.slot_for(NOW) - 2, driver.slot_for(NOW) + 3):
        stamp = driver.slot_stamp(slot)
        assert stamp.minute % 20 == 0 and stamp.second == 0
        assert family_proof_review_slot(stamp) == slot
        assert driver.slot_for(stamp) == slot
    assert driver.seconds_until_slot_run(NOW) == driver.CYCLE_OFFSET_SECONDS
    due = NOW + timedelta(seconds=driver.CYCLE_OFFSET_SECONDS)
    assert driver.seconds_until_slot_run(due) == 0
    late = NOW + timedelta(seconds=driver.CYCLE_OFFSET_SECONDS + 600)
    assert driver.seconds_until_slot_run(late) == 0


def test_scope_selection_replicates_bot_cycle_inputs(tmp_path):
    store = SQLiteHotStore(tmp_path / "scope.sqlite")
    try:
        _seed_candle(store, observed_at=NOW)
        assert driver.select_proof_scope(store, NOW) == _bot_scope(store, NOW)
        slot, scope = driver.select_proof_scope(store, NOW)
        assert slot == family_proof_review_slot(NOW)
        assert scope is not None and scope[:3] == STREAM
    finally:
        store.close()


def test_shadow_cycle_restores_state_and_live_row_matches(learning_store):
    _seed_candle(learning_store, observed_at=NOW)
    slot, scope = driver.select_proof_scope(learning_store, NOW)
    assert scope is not None
    _seed(learning_store, NOW, quote_count=0, horizon=scope[3])

    shadow = driver.run_promotion_cycle(
        learning_store, now=NOW, shadow=True)
    assert shadow["slot"] == slot and shadow["scope"] == scope
    assert shadow["status"] == "held" and shadow["selected_family"] is None
    assert shadow["row_sha256"] is not None
    assert shadow["promotion_detected"] is False
    # Restored exactly: the bot remains the only writer, so no row exists.
    assert _scope_row(learning_store, scope) is None

    live = driver.run_promotion_cycle(learning_store, now=NOW, shadow=False)
    assert live["row_sha256"] == shadow["row_sha256"]
    row = _scope_row(learning_store, scope)
    assert row is not None and row["evaluated_at"] == NOW.isoformat()
    assert row["status"] == "held"

    # A later shadow run over the live row restores it byte for byte.
    again = driver.run_promotion_cycle(
        learning_store, now=NOW, shadow=True)
    assert again["row_sha256"] == shadow["row_sha256"]
    assert _scope_row(learning_store, scope) == row


def test_shadow_canary_match_diff_and_computed(learning_store):
    _seed_candle(learning_store, observed_at=NOW)
    _, scope = driver.select_proof_scope(learning_store, NOW)
    assert scope is not None
    _seed(learning_store, NOW, quote_count=0, horizon=scope[3])
    shadow = driver.run_promotion_cycle(
        learning_store, now=NOW, shadow=True)

    matched = driver.run_shadow_canary(
        learning_store, slot=driver.slot_for(NOW),
        expected_sha256=shadow["row_sha256"])
    assert matched["canary_status"] == "MATCH"
    assert _scope_row(learning_store, scope) is None

    diffed = driver.run_shadow_canary(
        learning_store, slot=driver.slot_for(NOW),
        expected_sha256="0" * 64)
    assert diffed["canary_status"] == "DIFF"

    computed = driver.run_shadow_canary(
        learning_store, slot=driver.slot_for(NOW))
    assert computed["canary_status"] == "COMPUTED"
    assert computed["row_sha256"] == shadow["row_sha256"]


def test_shadow_cycle_never_rolls_back_a_promotion(learning_store):
    stamp = _horizon5_stamp()
    _seed_candle(learning_store, observed_at=stamp)
    _, scope = driver.select_proof_scope(learning_store, stamp)
    assert scope is not None and scope[3] == 5
    family = _seed(learning_store, stamp)  # the promoting seed

    report = driver.run_promotion_cycle(
        learning_store, now=stamp, shadow=True)
    assert report["promotion_detected"] is True
    assert report["status"] == "promoted_paper"
    assert report["selected_family"] == family
    # Left in place for human review -- not restored away.
    row = _scope_row(learning_store, scope)
    assert row is not None and row["status"] == "promoted_paper"
    assert row["selected_family"] == family


def test_bot_cycle_skips_review_when_worker_driven(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "gate.sqlite")
    try:
        monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        calls = []
        original = forward_audit.select_family_proof_scope

        def counting(eligible, *, hour_slot):
            calls.append(hour_slot)
            return original(eligible, hour_slot=hour_slot)

        monkeypatch.setattr(
            forward_audit, "select_family_proof_scope", counting)
        monkeypatch.setattr(driver, "worker_driven", lambda: False)
        monitor.run_once(now=NOW)
        assert len(calls) == 1

        calls.clear()
        monkeypatch.setattr(driver, "worker_driven", lambda: True)
        monitor.run_once(now=NOW)
        assert calls == []
    finally:
        store.close()


def test_run_forever_drives_each_slot_once(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "loop.sqlite")
    try:
        driven = []

        def stub(store_, *, now=None, shadow=False, promotion=None):
            driven.append(driver.slot_for(now))
            return {"slot": driver.slot_for(now), "scope": None,
                    "status": None}

        monkeypatch.setattr(driver, "run_promotion_cycle", stub)
        base = driver.slot_stamp(driver.slot_for(NOW))
        times = iter([
            base + timedelta(seconds=driver.CYCLE_OFFSET_SECONDS),
            base + timedelta(seconds=driver.CYCLE_OFFSET_SECONDS + 60),
            base + timedelta(minutes=20,
                             seconds=driver.CYCLE_OFFSET_SECONDS),
        ])
        driver.run_forever(
            store, shadow=True, max_cycles=2, sleep=lambda _s: None,
            now_fn=lambda: next(times))
        slot = driver.slot_for(NOW)
        assert driven == [slot, slot + 1]
    finally:
        store.close()
