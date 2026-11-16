"""Causal lead heads, durable rollover and actual accepted-card evidence."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app import call_board
from parlay_bot.discord_app.call_board import CallBoardService, live_calls_embed
from parlay_bot.discord_app.targeting import TargetSnapshot
from parlay_bot.perpetuals import advance_forecasts as module
from parlay_bot.perpetuals.advance_forecasts import (
    MODEL,
    TARGET_CONTRACT,
    AdvanceForecastMonitor,
    read_advance,
    read_advance_receipt,
    target_open,
)
from parlay_bot.perpetuals.features import live_examples, persisted_candles
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_checkpoints import LiveModelCheckpoints
from tests.test_perpetuals_forward_accuracy import seed

CLOCK = datetime(2026, 11, 15, 20, 1, tzinfo=UTC)


def setup(store, *, now=CLOCK):
    market = PerpetualMarketStore(store)
    seed(market, now=now)
    return market, AdvanceForecastMonitor(market)


def forecasts(store):
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM rival_advance_forecasts ORDER BY horizon_minutes"
        ).fetchall()]


def add_outcome(store, target, *, venue="hyperliquid", opened=110., closed=108., trades=20,
                observed=None):
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles
            (venue,contract,quote,interval_minutes,opened_at,closed_at,first_observed_at,
             open_price,high_price,low_price,close_price,volume_base,trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, "BTC", "USDC", 5, target.isoformat(),
             (target+timedelta(minutes=5, milliseconds=-1)).isoformat(),
             (observed or target+timedelta(minutes=5)).isoformat(),
             opened, max(opened, closed)+1, min(opened, closed)-1, closed, 10., trades),
        )


def test_advance_is_trained_on_the_next_candles_own_open_not_incumbent_label(learning_store):
    market, _monitor = setup(learning_store)
    rows = market.recent_candles(venue="hyperliquid", contract="BTC", quote="USDC",
                                 interval_minutes=5, count=1000, as_of=CLOCK)
    candles = persisted_candles(rows)
    incumbent = live_examples(candles, horizon_minutes=5)
    advance = live_examples(candles, horizon_minutes=5, lead_minutes=5)
    assert {s.label_up for s in incumbent} == {0, 1}
    assert {s.label_up for s in advance} == {1}
    assert len(advance) == len(incumbent)-1
    assert all(s.issued_at < s.label_known_at <= CLOCK for s in advance)
    # The label's first observation belongs to the candle TWO bars ahead.
    assert advance[0].label_known_at == candles[41].observed_at
    assert advance[0].features == incumbent[0].features
    with pytest.raises(ValueError):
        live_examples(candles, horizon_minutes=5, lead_minutes=10)


def test_all_horizons_issue_early_keep_exact_source_and_survive_restart(learning_store):
    market, monitor = setup(learning_store)
    assert monitor.run_once(now=CLOCK) == (3, 0)
    original = forecasts(learning_store)
    assert [r["horizon_minutes"] for r in original] == [5, 15, 60]
    for row in original:
        assert row["model_version"] == MODEL
        assert row["target_contract"] == TARGET_CONTRACT
        assert row["issued_at"] == row["committed_at"] == CLOCK.isoformat()
        assert row["issued_at"] < row["target_opened_at"] < row["target_closed_at"]
        assert row["target_opened_at"] == target_open(CLOCK, row["horizon_minutes"]).isoformat()
        assert 0 < row["probability_up"] < 1
        assert len(row["feature_sha256"]) == len(row["weights_sha256"]) == 64
    restarted = AdvanceForecastMonitor(market)
    assert restarted.run_once(now=CLOCK+timedelta(seconds=1)) == (0, 0)
    assert forecasts(learning_store) == original
    snapshot = read_advance(market, asset="BTC", horizon=5, now=CLOCK,
                            venue="hyperliquid", contract="BTC", quote="USDC")
    assert snapshot["next"]["probability_up"] == original[0]["probability_up"]
    # A read cannot create forecasts, readiness attempts, or labels.
    assert forecasts(learning_store) == original


def test_incumbent_and_advance_checkpoint_target_contracts_are_isolated(learning_store):
    market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    rows = market.recent_candles(venue="hyperliquid", contract="BTC", quote="USDC",
                                 interval_minutes=5, count=1000, as_of=CLOCK)
    incumbent = LiveModelCheckpoints(learning_store)
    incumbent.fit(live_examples(persisted_candles(rows), horizon_minutes=5), as_of=CLOCK)
    with learning_store.connect() as conn:
        saved = conn.execute(
            "SELECT target_contract,horizon_minutes,weights_sha256 FROM rival_live_model_checkpoints"
        ).fetchall()
    assert len(saved) == 4
    five = [r for r in saved if r["horizon_minutes"] == 5]
    assert len({r["target_contract"] for r in five}) == 2
    assert len({r["weights_sha256"] for r in five}) == 2


def test_batch_backfill_cannot_become_causal_lead_training(learning_store):
    market, monitor = setup(learning_store)
    with learning_store.connect() as conn:
        conn.execute("UPDATE perp_market_candles SET first_observed_at=?", (CLOCK.isoformat(),))
    assert monitor.run_once(now=CLOCK) == (0, 0)
    assert forecasts(learning_store) == []
    result = read_advance(market, asset="BTC", horizon=5, now=CLOCK)
    assert result["next"] is None
    assert "genuinely observed" in result["reason"]


@pytest.mark.parametrize("case", ["stale", "budget", "wrong_source"])
def test_no_issue_reasons_are_persisted_without_manufacturing_calls(learning_store, case):
    _market, monitor = setup(learning_store)
    if case == "stale":
        stamp, budget, reason = CLOCK+timedelta(minutes=10), 45, "stale_candle"
    elif case == "budget":
        stamp, budget, reason = CLOCK, 0.000000001, "cycle_budget_exhausted"
    else:
        with learning_store.connect() as conn:
            conn.execute("UPDATE perp_market_candles SET quote='USD'")
        stamp, budget, reason = CLOCK, 45, "source_mismatch"
    assert monitor.run_once(now=stamp, budget_seconds=budget) == (0, 0)
    with learning_store.connect() as conn:
        reasons = conn.execute("SELECT reason FROM rival_advance_readiness").fetchall()
    assert [r["reason"] for r in reasons] == [reason]*3
    assert forecasts(learning_store) == []


@pytest.mark.parametrize("trades", [0, 20])
def test_native_outcomes_settle_once_and_never_rewrite_forecasts(learning_store, trades):
    _market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    before = forecasts(learning_store)
    target = target_open(CLOCK, 5)
    # Another venue's outcome cannot settle this forecast.
    add_outcome(learning_store, target, venue="binance")
    assert monitor.settle(target+timedelta(minutes=6)) == 0
    add_outcome(learning_store, target, trades=trades)
    assert monitor.settle(target+timedelta(minutes=4)) == 0
    assert monitor.settle(target+timedelta(minutes=6)) == 1
    assert monitor.settle(target+timedelta(minutes=7)) == 0
    after = forecasts(learning_store)
    for key in ("issued_at", "committed_at", "probability_up", "baseline_up",
                "feature_sha256", "weights_sha256", "target_opened_at"):
        assert before[0][key] == after[0][key]
    assert after[0]["outcome_up"] == (0 if trades else None)
    assert after[0]["outcome_status"] == ("observed" if trades else "no_trades")
    assert after[0]["model_brier"] == (before[0]["probability_up"]**2 if trades else None)
    report = read_advance_receipt(learning_store, target+timedelta(minutes=6))
    assert report["status"] == "recorded"
    assert report["qualified"] is False
    assert report["recent_forecasts"][0]["seconds_of_advance_warning"] > 0


def test_wrong_outcome_chronology_is_not_scored(learning_store):
    _market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    target = target_open(CLOCK, 5)
    add_outcome(learning_store, target, observed=CLOCK-timedelta(seconds=1))
    assert monitor.settle(target+timedelta(minutes=6)) == 0
    assert all(r["outcome_up"] is None for r in forecasts(learning_store))


def test_rollover_keeps_the_original_advance_and_does_not_relabel_it(learning_store):
    market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    earlier = read_advance(market, asset="BTC", horizon=5, now=CLOCK)["next"]
    rolled = read_advance(market, asset="BTC", horizon=5, now=CLOCK+timedelta(minutes=5))
    assert rolled["next"] is None
    assert rolled["previous"] == earlier
    # The same event is retained even if incumbent directions would change.
    assert forecasts(learning_store)[0]["probability_up"] == earlier["probability_up"]
    wrong = read_advance(market, asset="BTC", horizon=5, now=CLOCK,
                         venue="binance", contract="BTCUSDT", quote="USDT")
    assert wrong["next"] is None


def test_a_commit_after_the_deadline_cannot_be_claimed_as_an_advance(learning_store, monkeypatch):
    market, monitor = setup(learning_store)
    target = target_open(CLOCK, 5)
    times = iter([CLOCK, CLOCK, CLOCK, target+timedelta(seconds=1)])

    class Clock(datetime):
        @classmethod
        def now(cls, _tz=None):
            return next(times, target+timedelta(seconds=1))

    monkeypatch.setattr(module, "datetime", Clock)
    assert monitor.run_once()[0] == 2  # 15/60 remain genuinely pre-target
    first = forecasts(learning_store)[0]
    assert first["issued_at"] < first["target_opened_at"] < first["committed_at"]
    assert first["outcome_status"] == "commit_after_deadline"
    assert read_advance(market, asset="BTC", horizon=5, now=CLOCK)["next"] is None
    add_outcome(learning_store, target)
    assert monitor.settle(target+timedelta(minutes=6)) == 0


def test_pre_start_status_requires_observed_commit_and_cannot_be_backfilled(learning_store):
    market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_advance_forecasts SET committed_at=NULL")
    assert read_advance(market, asset="BTC", horizon=5, now=CLOCK)["next"] is None
    assert AdvanceForecastMonitor(market).run_once(now=CLOCK)[0] == 0
    assert all(r["committed_at"] is None for r in forecasts(learning_store))


def test_live_card_names_lead_model_source_confidence_and_target(learning_store, monkeypatch):
    market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    # A missing incumbent must not hide a real advance forecast.
    monkeypatch.setattr(call_board, "recent_watches", lambda *_a, **_k: [])
    card, digest = live_calls_embed(market, "BTC", now=CLOCK)
    assert len(digest) == 64
    assert len(card.advance_forecasts) == 3
    for field in card.fields[:3]:
        assert "NEXT · UP · Confidence:" in field.value
        assert MODEL in field.value
        assert "own open" in field.value
        assert "NO TRADE CALL" in field.value
        assert "hyperliquid" in field.value
        assert len(field.value) <= 1024
    rolled, _ = live_calls_embed(market, "BTC", now=CLOCK+timedelta(minutes=5))
    assert "Prior advance" in rolled.fields[0].value
    assert "unchanged" in rolled.fields[0].value


@pytest.mark.asyncio
async def test_deliveries_record_only_the_original_rows_discord_actually_accepted(learning_store):
    now = datetime.now(UTC)
    market, monitor = setup(learning_store, now=now)
    monitor.run_once(now=now)
    card, fingerprint = live_calls_embed(market, "BTC", now=now)
    assert card.advance_forecasts
    edit = AsyncMock()
    message = SimpleNamespace(edit=edit)
    channel = SimpleNamespace(get_partial_message=lambda _id: message)
    service = CallBoardService(SimpleNamespace(get_partial_messageable=lambda _id: channel),
                              learning_store, market)
    service.initialize()
    service._save(100, "exchange", "BTC", 0, 200, 300, "old")
    row = service._rows()[0]
    edit.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await service._edit(row, card, fingerprint)
    assert read_advance_receipt(learning_store, now)["deliveries"] == []
    edit.side_effect = None
    assert await service._edit(row, card, fingerprint)
    assert await service._edit(row, card, fingerprint)  # same old cache row
    with learning_store.connect() as conn:
        delivered = conn.execute("SELECT * FROM rival_advance_deliveries").fetchall()
    assert len(delivered) == len(card.advance_forecasts)
    assert all(r["card_fingerprint"] == fingerprint for r in delivered)
    assert all(r["issued_at"] <= r["delivered_at"] < r["target_opened_at"] for r in delivered)
    report = read_advance_receipt(learning_store, datetime.now(UTC))
    assert all(r["seconds_of_advance_warning"] > 0 for r in report["recent_deliveries"])
    assert all(r["issuance_to_delivery_seconds"] >= 0 for r in report["recent_deliveries"])
    assert "channel_id" not in str(report)
    assert report["discord_delivery_is_api_acceptance_not_independent_observation"] is True


def test_missing_schema_is_unavailable_and_a_read_does_not_run_migrations(learning_store):
    market = PerpetualMarketStore(learning_store)
    assert read_advance(market, asset="BTC", horizon=5, now=CLOCK)["next"] is None
    assert read_advance_receipt(learning_store, CLOCK)["status"] == "read_unavailable"


def test_live_cycles_do_not_repeat_schema_ddl(learning_store, monkeypatch):
    _market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    real = monitor.discovery.initialize
    monkeypatch.setattr(monitor.discovery, "initialize",
                        lambda: pytest.fail("repeated discovery schema on a live cycle"))
    assert monitor.run_once(now=CLOCK+timedelta(seconds=1)) == (0, 0)
    monkeypatch.setattr(monitor.discovery, "initialize", real)


def test_a_failed_source_cannot_starve_another_asset(learning_store, monkeypatch):
    market, monitor = setup(learning_store)
    with learning_store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles
            SELECT venue,'ETH',quote,interval_minutes,opened_at,closed_at,first_observed_at,
                   open_price,high_price,low_price,close_price,volume_base,trades
            FROM perp_market_candles WHERE contract='BTC'"""
        )
    real = market.recent_candles

    def read(**kw):
        if kw["contract"] == "BTC":
            raise ValueError("unavailable BTC source")
        return real(**kw)

    monkeypatch.setattr(market, "recent_candles", read)
    assert monitor.run_once(now=CLOCK) == (3, 0)
    assert {r["asset"] for r in forecasts(learning_store)} == {"ETH"}
    with learning_store.connect() as conn:
        reasons = conn.execute("SELECT reason FROM rival_advance_readiness WHERE asset='BTC'")
        assert {r["reason"] for r in reasons.fetchall()} == {"source_read_or_fit_failed"}


def test_slow_fitting_cannot_issue_after_the_target_started(learning_store, monkeypatch):
    _market, monitor = setup(learning_store)
    target = target_open(CLOCK, 5)
    times = iter([CLOCK, CLOCK])

    class Clock(datetime):
        @classmethod
        def now(cls, _tz=None):
            return next(times, target+timedelta(seconds=1))

    monkeypatch.setattr(module, "datetime", Clock)
    assert monitor.run_once()[0] == 2
    assert all(r["horizon_minutes"] != 5 for r in forecasts(learning_store))
    with learning_store.connect() as conn:
        reason = conn.execute("SELECT reason FROM rival_advance_readiness WHERE horizon_minutes=5")
        assert reason.fetchone()["reason"] == "target_already_started"


def test_current_and_next_card_fit_discord_limits_and_preserve_the_prior_call(learning_store,
                                                                          monkeypatch):
    market, monitor = setup(learning_store)
    monitor.run_once(now=CLOCK)
    add_outcome(learning_store, target_open(CLOCK, 5)-timedelta(minutes=5))
    now = CLOCK+timedelta(minutes=5)
    monitor.run_once(now=now)
    monkeypatch.setattr(call_board, "recent_watches", lambda *_a, **_k: [])

    def snapshot(_market, *, asset, horizon_minutes, now):
        return TargetSnapshot(
            asset=asset, horizon_minutes=horizon_minutes, direction="UP", probability=.71,
            detail="Research only", venue="hyperliquid", price=80123.5,
            issued_at=now-timedelta(seconds=10), ends_at=target_open(now, horizon_minutes),
            status="unqualified_model", model_version="incumbent:"+"a"*64,
        )

    monkeypatch.setattr(call_board, "target_snapshot", snapshot)
    card, _ = live_calls_embed(market, "BTC", now=now)
    assert len(card.advance_forecasts) == 3
    for field in card.fields[:3]:
        assert "Confidence: 71.0%" in field.value
        assert "NEXT ·" in field.value
        assert "Prior advance" in field.value
        assert len(field.value) <= 1024


@pytest.mark.asyncio
async def test_cancelled_advance_wait_does_not_start_a_duplicate_model_thread():
    import asyncio
    import threading

    from parlay_bot.perpetuals.autonomous import AutonomousResearch, StepStillRunning

    entered, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append(True)
        entered.set()
        release.wait(timeout=5)
        return 0, 0

    research = object.__new__(AutonomousResearch)
    research._step_threads = {}
    research.advance_forward = SimpleNamespace(run_once=work)
    running = asyncio.create_task(research.advance_cycle())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        with pytest.raises(StepStillRunning):
            await research.advance_cycle()
        assert calls == [True]
    finally:
        release.set()
        await asyncio.wait_for(research._step_threads["advance.run_once"], timeout=1)
