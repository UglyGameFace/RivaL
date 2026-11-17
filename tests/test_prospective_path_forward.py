"""Prospective first-passage paper forecasts: SQLite/Postgres, real time fences."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.first_passage import trace_barriers
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.learning_health_receipt import _path_target_progress
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.path_forward import (
    HORIZONS,
    MAX_SETTLE,
    TARGET_VERSION,
    ProspectivePathLedger,
    empirical_paths,
)


def _candles(*, count=400, start=None):
    stamp=start or datetime(2026,10,28,tzinfo=UTC)
    values=[]
    for index in range(count):
        opened=stamp+timedelta(minutes=5*index)
        price=100+0.18*math.sin(index/3)+0.04*math.sin(index/9)
        values.append(MarketCandle(
            venue="hyperliquid",contract="BTC",quote="USDC",
            interval_minutes=5,opened_at=opened,
            closed_at=opened+timedelta(minutes=5,milliseconds=-1),
            observed_at=opened+timedelta(minutes=5,seconds=1),
            open_price=price,high_price=price+.07,low_price=price-.07,
            close_price=price,volume_base=10.0,trades=8,
        ))
    return values


def _row(c):
    return (c.venue,c.contract,c.quote,5,c.opened_at.isoformat(),
            c.closed_at.isoformat(),c.observed_at.isoformat(),
            c.open_price,c.high_price,c.low_price,c.close_price,
            c.volume_base,c.trades)


def _future_path(last, *, steps=3):
    return [replace(
        last, opened_at=last.opened_at+timedelta(minutes=5*j),
        closed_at=last.opened_at+timedelta(minutes=5*(j+1), milliseconds=-1),
        observed_at=last.opened_at+timedelta(minutes=5*(j+1), seconds=2),
        high_price=last.close_price+.001, low_price=last.close_price-.001,
    ) for j in range(2, steps+2)]


@pytest.mark.parametrize("blocked", ["missing", "observed_before_issue", "future_receipt"])
def test_unscoreable_old_paths_cannot_starve_new_complete_paths(learning_store, blocked):
    """A full oldest batch without valid source evidence must not stop progress."""
    PerpetualMarketStore(learning_store).initialize()
    ledger=ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    options=empirical_paths(candles,horizon=15,now=issued)
    future=_future_path(last)
    now=future[-1].observed_at+timedelta(minutes=1)
    old=[]
    with learning_store.connect() as conn:
        for index in range(MAX_SETTLE//2+1):
            shift=timedelta(hours=-24, minutes=20*index)
            prior=replace(last, opened_at=last.opened_at+shift,
                          closed_at=last.closed_at+shift,
                          observed_at=last.observed_at+shift)
            old.append(prior)
            assert ledger.issue(conn,candles=[prior],asset="BTC",horizon=15,
                                now=issued+shift,issued_at=issued+shift,options=options)==2
            if blocked != "missing":
                for bar in _future_path(prior):
                    observed=(issued+shift-timedelta(seconds=1)
                              if blocked == "observed_before_issue"
                              else now+timedelta(minutes=1))
                    conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 _row(replace(bar,observed_at=observed)))
        assert ledger.issue(conn,candles=candles,asset="BTC",horizon=15,
                            now=issued,issued_at=issued,options=options)==2
        for bar in future:
            conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         _row(bar))
        frozen=conn.execute("""SELECT reference_opened_at,issued_at,training_sha256,
                                     tp1_probability,tp2_probability
                              FROM rival_forward_paths ORDER BY reference_opened_at,side""").fetchall()
    before=_path_target_progress(learning_store,now)
    assert before["status"]=="settlement_backlog"
    assert before["due_pending_paths_14d"]==2*len(old)+2
    sample=before["settlement_sample"]
    assert sample["status"]=="sampled" and sample["read_only"]
    assert sample["sample_only_not_backlog_census"] and sample["sides_are_correlated"]
    assert sample["by_reason"]["ready_for_settlement"]>=1
    if blocked == "missing":
        old_gap = next(
            example for example in sample["examples"]
            if example["reason"] == "missing_source_candles"
        )
        assert old_gap["missing_candle_opened_at"] == [
            (old[0].opened_at + timedelta(minutes=minutes)).isoformat()
            for minutes in (10, 15, 20)
        ]
    else:
        assert all(
            not example["missing_candle_opened_at"]
            for example in sample["examples"]
        )
    assert sample["by_reason"][{
        "missing": "missing_source_candles",
        "observed_before_issue": "source_seen_before_issuance",
        "future_receipt": "source_not_yet_observed",
    }[blocked]]>=1
    assert ledger.settle(now=now)=={"settled":2,"ambiguous":0}
    progress=_path_target_progress(learning_store,now)
    assert progress["status"]=="observed"
    assert progress["settled_close_targets_24h"]==2
    assert progress["due_pending_paths_14d"]==2*len(old)
    # A new worker must not need a process-local skip list to retain progress.
    restarted=ProspectivePathLedger(learning_store)
    assert restarted.settle(now=now)=={"settled":0,"ambiguous":0}
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_forward_paths WHERE state='pending'").fetchone()["n"] > MAX_SETTLE
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_forward_path_targets WHERE state='settled'").fetchone()["n"]==2
        after=conn.execute("""SELECT reference_opened_at,issued_at,training_sha256,
                                    tp1_probability,tp2_probability
                             FROM rival_forward_paths ORDER BY reference_opened_at,side""").fetchall()
        assert [dict(row) for row in after]==[dict(row) for row in frozen]
        if blocked == "missing":
            for bar in _future_path(old[0]):
                conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             _row(bar))
    if blocked == "missing":
        # A source that later supplies its original complete future path is
        # reconsidered; incomplete prospects are not discarded as losses.
        assert restarted.settle(now=now)=={"settled":2,"ambiguous":0}


@pytest.mark.parametrize("delay", [timedelta(), timedelta(days=3)], ids=["current", "backlog"])
def test_ready_path_batch_remains_bounded_and_drains_across_worker_restarts(learning_store,
                                                                          monkeypatch, delay):
    from parlay_bot.perpetuals import path_forward

    monkeypatch.setattr(path_forward,"MAX_SETTLE",2)
    PerpetualMarketStore(learning_store).initialize()
    ledger=ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    options=empirical_paths(candles,horizon=15,now=issued)
    now=_future_path(last)[-1].observed_at+timedelta(minutes=1)+delay
    with learning_store.connect() as conn:
        for asset in ("BTC","ETH","SOL"):
            source=replace(last,contract=asset)
            assert ledger.issue(conn,candles=[source],asset=asset,horizon=15,
                                now=issued,issued_at=issued,options=options)==2
            for bar in _future_path(source):
                conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             _row(bar))
    for expected in (4,2):
        assert ProspectivePathLedger(learning_store).settle(now=now)=={
            "settled":expected,"ambiguous":0,
        }
    assert ledger.settle(now=now)=={"settled":0,"ambiguous":0}
    progress=_path_target_progress(learning_store,now)
    assert progress["settled_close_targets_24h"]==(6 if not delay else 0)
    assert progress["resolved_path_sides_24h"]==6
    assert progress["pending_paths_14d"]==progress["due_pending_paths_14d"]==0


@pytest.mark.parametrize("legacy_paths", [0, MAX_SETTLE+1], ids=["empty-backlog", "older-backlog"])
def test_one_full_issuance_cycle_drains_across_settles_without_unbounded_backlog(learning_store,
                                                                             legacy_paths):
    """40 streams x 3 horizons x 2 sides = 240 sides per full cycle.

    Settlement is bounded at MAX_SETTLE=100 paths (200 sides) per call, so
    one full cycle's issuance no longer fits in a single settle: the first
    settle caps at 200 sides and the remainder drains on the next settle(s).
    The backlog is bounded and drains when no new issuance lands; it does
    not grow without bound. (Admitting 5m is what pushed a full cycle past
    the per-settle bound; MAX_SETTLE is deliberately unchanged.)
    """
    from parlay_bot.perpetuals.forward_audit import MAX_STREAMS_PER_CYCLE

    PerpetualMarketStore(learning_store).initialize()
    ledger=ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    options={h:empirical_paths(candles,horizon=h,now=issued) for h in HORIZONS}
    future=_future_path(last,steps=12)
    now=future[-1].observed_at+timedelta(minutes=1)
    venues=("hyperliquid","binance","dydx","bitget","gate","okx",
            "kucoin","deribit","lighter","phemex")
    streams=0
    with learning_store.connect() as conn:
        for index in range(legacy_paths):
            shift = timedelta(days=-3, minutes=20*index)
            prior = replace(last, opened_at=last.opened_at+shift,
                            closed_at=last.closed_at+shift,
                            observed_at=last.observed_at+shift)
            assert ledger.issue(conn, candles=[prior], asset="BTC", horizon=15,
                                now=issued+shift, issued_at=issued+shift,
                                options=options[15]) == 2
            for bar in _future_path(prior):
                conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             _row(bar))
        conn.execute("DELETE FROM rival_forward_path_targets")
        for venue in venues:
            for asset in ("BTC","ETH","SOL","XRP"):
                source=replace(last,venue=venue,contract=asset)
                streams+=1
                for horizon in HORIZONS:
                    assert ledger.issue(conn,candles=[source],asset=asset,horizon=horizon,
                                        now=issued,issued_at=issued,options=options[horizon])==2
                for bar in _future_path(source,steps=12):
                    conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 _row(bar))
    assert streams==MAX_STREAMS_PER_CYCLE
    # First settle caps at the 100-path bound (200 sides). Empty backlog:
    # 240 issued sides - 200 settled = 40 pending (the newest 5m paths are
    # last in the current queue, so they are what remains). Older backlog:
    # 101 legacy paths (202 sides, targets deleted above) + 240 new sides
    # - 200 settled = 242 pending.
    assert ledger.settle(now=now)=={"settled":200,"ambiguous":0}
    progress=_path_target_progress(learning_store,now)
    assert progress["settled_close_targets_24h"]==(200 if not legacy_paths else 160)
    assert progress["pending_paths_14d"]==progress["due_pending_paths_14d"]==(
        242 if legacy_paths else 40)
    # The remainder drains on the next settle(s); nothing is left behind.
    # Empty backlog: 40 sides. Older backlog: 200 more sides, then 42.
    assert ProspectivePathLedger(learning_store).settle(now=now)=={
        "settled":200 if legacy_paths else 40,"ambiguous":0}
    assert ledger.settle(now=now)=={
        "settled":42 if legacy_paths else 0,"ambiguous":0}
    if legacy_paths:
        assert ledger.settle(now=now)=={"settled":0,"ambiguous":0}
    progress=_path_target_progress(learning_store,now)
    assert progress["pending_paths_14d"]==progress["due_pending_paths_14d"]==0


def test_current_targets_settle_while_legacy_backlog_drains_across_restarts(learning_store,
                                                                         monkeypatch):
    """Old eligible paths must not consume every cycle before new heads score."""
    from parlay_bot.perpetuals import path_forward

    monkeypatch.setattr(path_forward, "MAX_SETTLE", 4)
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = empirical_paths(candles, horizon=15, now=issued)
    with learning_store.connect() as conn:
        for index in range(6):
            shift = timedelta(days=-3, minutes=20*index)
            prior = replace(last, opened_at=last.opened_at+shift,
                            closed_at=last.closed_at+shift,
                            observed_at=last.observed_at+shift)
            assert ledger.issue(conn, candles=[prior], asset="BTC", horizon=15,
                                now=issued+shift, issued_at=issued+shift,
                                options=options) == 2
            for bar in _future_path(prior):
                conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             _row(bar))
        # Before close-only heads were introduced, the real ledger already
        # held prospective barrier forecasts. Never fabricate companion heads
        # for these older records when they later become scoreable.
        conn.execute("DELETE FROM rival_forward_path_targets")
        frozen = [dict(row) for row in conn.execute(
            """SELECT venue,contract,reference_opened_at,issued_at,side,
                      tp1_probability,tp2_probability,training_sha256
               FROM rival_forward_paths ORDER BY reference_opened_at,side""",
        ).fetchall()]
    for cycle in range(6):
        shift = timedelta(minutes=20*cycle)
        current = replace(last, opened_at=last.opened_at+shift,
                          closed_at=last.closed_at+shift,
                          observed_at=last.observed_at+shift)
        with learning_store.connect() as conn:
            for asset in ("BTC", "ETH", "SOL"):
                source = replace(current, contract=asset)
                assert ledger.issue(conn, candles=[source], asset=asset, horizon=15,
                                    now=issued+shift, issued_at=issued+shift,
                                    options=options) == 2
                for bar in _future_path(source):
                    conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 _row(bar))
        now = _future_path(current)[-1].observed_at + timedelta(minutes=1)
        result = ProspectivePathLedger(learning_store).settle(now=now)
        assert result == {"settled": 8, "ambiguous": 0}
        with learning_store.connect() as conn:
            pending_current = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_forward_path_targets WHERE state='pending'",
            ).fetchone()["n"]
            assert pending_current == 0, "old paths consumed the current targets' batch"
            pending_old = conn.execute(
                """SELECT COUNT(*) AS n FROM rival_forward_paths
                   WHERE state='pending' AND issued_at<?""", (issued.isoformat(),),
            ).fetchone()["n"]
            assert pending_old == 2*(5-cycle)
    with learning_store.connect() as conn:
        after = [dict(row) for row in conn.execute(
            """SELECT venue,contract,reference_opened_at,issued_at,side,
                      tp1_probability,tp2_probability,training_sha256
               FROM rival_forward_paths WHERE issued_at<?
               ORDER BY reference_opened_at,side""", (issued.isoformat(),),
        ).fetchall()]
        assert after == frozen
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_forward_path_targets").fetchone()["n"] == 36
    progress = _path_target_progress(learning_store, now)
    assert progress["settled_close_targets_24h"] == 36
    assert progress["resolved_path_sides_24h"] == 48
    assert progress["due_pending_paths_14d"] == 0
    assert ledger.settle(now=now) == {"settled": 0, "ambiguous": 0}


def test_path_receipt_distinguishes_no_issuance_future_targets_and_backlog(learning_store):
    from contextlib import contextmanager
    from types import SimpleNamespace

    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    assert _path_target_progress(learning_store,issued)["status"]=="not_initialized"
    PerpetualMarketStore(learning_store).initialize()
    ledger=ProspectivePathLedger(learning_store)
    ledger.initialize()

    @contextmanager
    def connect():
        with learning_store.connect() as conn:
            class ReadOnly:
                def execute(self,sql,params=()):
                    assert sql.lstrip().upper().startswith("SELECT ")
                    return conn.execute(sql,params)
            yield ReadOnly()

    reader=SimpleNamespace(connect=connect,database=learning_store.database)
    empty=_path_target_progress(reader,issued)
    assert empty["status"]=="no_recent_issuance"
    assert empty["path_forecasts_issued_24h"]==empty["due_pending_paths_14d"]==0
    assert empty["settlement_sample"]["status"]=="no_due_pending_paths"
    with learning_store.connect() as conn:
        assert ledger.issue(conn,candles=candles,asset="BTC",horizon=60,
                            now=issued,issued_at=issued)==2
    waiting=_path_target_progress(reader,issued)
    assert waiting["status"]=="waiting_for_forward_outcomes"
    assert waiting["path_forecasts_issued_24h"]==waiting["pending_paths_14d"]==2
    assert waiting["due_pending_paths_14d"]==0
    future=_future_path(last,steps=12)
    now=future[-1].observed_at+timedelta(minutes=1)
    backlog=_path_target_progress(reader,now)
    assert backlog["status"]=="settlement_backlog"
    assert backlog["due_pending_paths_14d"]==2
    assert backlog["oldest_due_target_at"]==future[-1].opened_at.isoformat()
    assert backlog["settlement_sample"]["by_reason"]=={"missing_source_candles":1}
    # All bars from a different contract/quote cannot settle this prospect.
    with learning_store.connect() as conn:
        for bar in future:
            conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         _row(replace(bar,quote="USD")))
    assert ledger.settle(now=now)=={"settled":0,"ambiguous":0}
    with learning_store.connect() as conn:
        for bar in future:
            conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         _row(bar))
    ready=_path_target_progress(reader,now)["settlement_sample"]
    assert ready["by_reason"]=={"ready_for_settlement":1}
    assert ledger.settle(now=now)=={"settled":2,"ambiguous":0}
    observed=_path_target_progress(reader,now)
    assert observed["status"]=="observed"
    assert observed["settled_close_targets_24h"]==2
    assert observed["horizons"][0]["horizon_minutes"]==60
    assert observed["read_only"] and not observed["counts_are_independent_targets"]
    assert observed["does_not_prove_prediction_edge"] and not observed["after_cost_qualified"]


def test_causal_source_matched_training_and_conservative_first_passage():
    candles=_candles()
    now=candles[-1].observed_at+timedelta(seconds=2)
    for horizon in HORIZONS:
        results=empirical_paths(candles,horizon=horizon,now=now)
        assert set(results)=={"long","short"}
        for item in results.values():
            assert item["training_count"]>=25
            assert item["cohort_count"]>=12
            assert 0<item["tp2_probability"]<=item["tp1_probability"]<1
            assert item["p10"]<=item["p50"]<=item["p90"]
            assert item["absolute_move_p50"]>=0
            assert item["absolute_move_baseline"]>=0
            assert item["close_target_training_count"]>=item["training_count"]
            assert len(item["close_target_training_sha256"])==64
            for kind in ("direction", "early_move"):
                assert 0<item[f"{kind}_probability"]<1
                assert 0<item[f"{kind}_baseline"]<1
            assert len(item["training_sha256"])==64
    future=replace(candles[-1],observed_at=now+timedelta(minutes=15))
    with pytest.raises(ValueError,match="future"):
        empirical_paths(candles[:-1]+[future],horizon=15,now=now)
    other=replace(candles[-1],venue="okx")
    with pytest.raises(ValueError,match="cross-market"):
        empirical_paths(candles[:-1]+[other],horizon=15,now=now)
    assert trace_barriers(
        entry=100.,sigma_horizon=.01,side="long",
        future=[{"high":103.,"low":97.}],
    ) is None


def test_real_forward_settlement_rejects_backfill_and_duplicate_predictions(learning_store):
    hot=learning_store
    market=PerpetualMarketStore(hot)
    market.initialize()
    ledger=ProspectivePathLedger(hot)
    ledger.initialize()
    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    with hot.connect() as conn:
        assert ledger.issue(conn,candles=candles,asset="BTC",horizon=15,
                            now=issued,issued_at=issued)==2
        assert ledger.issue(conn,candles=candles,asset="BTC",horizon=15,
                            now=issued,issued_at=issued)==0
    assert ledger.settle(now=issued+timedelta(minutes=1))=={
        "settled":0,"ambiguous":0,
    }
    future=[]
    # This partial bar opened BEFORE issuance. It may show both barriers
    # touched, but is deliberately excluded from the scored future path.
    partial_open=last.opened_at+timedelta(minutes=5)
    partial=MarketCandle(
        venue="hyperliquid",contract="BTC",quote="USDC",
        interval_minutes=5,opened_at=partial_open,
        closed_at=partial_open+timedelta(minutes=5,milliseconds=-1),
        observed_at=partial_open+timedelta(minutes=5,seconds=2),
        open_price=last.close_price,
        high_price=last.close_price*1.5,low_price=last.close_price*.5,
        close_price=last.close_price,volume_base=10.0,trades=8,
    )
    for j in range(2,5):
        opened=last.opened_at+timedelta(minutes=5*j)
        future.append(MarketCandle(
            venue="hyperliquid",contract="BTC",quote="USDC",
            interval_minutes=5,opened_at=opened,
            closed_at=opened+timedelta(minutes=5,milliseconds=-1),
            observed_at=opened+timedelta(minutes=5,seconds=2),
            open_price=last.close_price,high_price=last.close_price+.001,
            low_price=last.close_price-.001,close_price=last.close_price,
            volume_base=10.0,trades=8,
        ))
    with hot.connect() as conn:
        for candle in [partial,*future]:
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(candle),
            )
        # One retrospectively supplied candle makes the ENTIRE path
        # ineligible. The original forecast probability is immutable.
        conn.execute(
            """UPDATE perp_market_candles SET first_observed_at=?
               WHERE venue='hyperliquid' AND opened_at=?""",
            ((issued-timedelta(seconds=1)).isoformat(),
             future[1].opened_at.isoformat()),
        )
    later=future[-1].observed_at+timedelta(minutes=1)
    assert ledger.settle(now=later)=={"settled":0,"ambiguous":0}
    with hot.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_forward_path_targets WHERE state='settled'"
        ).fetchone()["n"] == 0
        conn.execute(
            """UPDATE perp_market_candles SET first_observed_at=?
               WHERE venue='hyperliquid' AND opened_at=?""",
            (future[1].observed_at.isoformat(),future[1].opened_at.isoformat()),
        )
    assert ledger.settle(now=later)=={"settled":2,"ambiguous":0}
    assert ledger.settle(now=later)=={"settled":0,"ambiguous":0}
    task_scores=ledger.target_report(asset="BTC",horizon=15,now=later)
    assert len(task_scores)==2
    for report in task_scores.values():
        assert report["direction"]["targets"]==1
        assert report["early_close_move"]["targets"]==1
        assert report["absolute_terminal_move"]["targets"]==1
        assert report["qualified_for_live_use"] is False
        assert report["after_cost_qualified"] is False
    summary=ledger.report(asset="BTC",horizon=15,now=later)
    assert len(summary)==2
    assert all(not value["qualified_research"]
               and not value["after_cost_qualified"]
               and not value["live_approved"]
               for value in summary.values())
    with hot.connect() as conn:
        rows=conn.execute(
            """SELECT state,training_sha256,tp1_probability,
                      tp1_before_stop,tp2_before_stop
               FROM rival_forward_paths ORDER BY side""",
        ).fetchall()
        assert len(rows)==2
        assert all(x["state"]=="settled" and x["tp1_before_stop"]==0
                   and x["tp2_before_stop"]==0 and len(x["training_sha256"])==64
                   and 0<x["tp1_probability"]<1 for x in rows)


def test_same_bar_tp_and_stop_are_ambiguous_not_model_wins(learning_store):
    hot=learning_store
    PerpetualMarketStore(hot).initialize()
    ledger=ProspectivePathLedger(hot)
    ledger.initialize()
    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    with hot.connect() as conn:
        assert ledger.issue(conn,candles=candles,asset="BTC",horizon=15,
                            now=issued,issued_at=issued)==2
        for j in range(2,5):
            opened=last.opened_at+timedelta(minutes=5*j)
            c=MarketCandle(
                venue=last.venue,contract=last.contract,quote=last.quote,
                interval_minutes=5,opened_at=opened,
                closed_at=opened+timedelta(minutes=5,milliseconds=-1),
                observed_at=opened+timedelta(minutes=5,seconds=2),
                open_price=last.close_price,high_price=last.close_price*1.5,
                low_price=last.close_price*.5,close_price=last.close_price,
                volume_base=10.0,trades=8,
            )
            conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         _row(c))
    now=last.opened_at+timedelta(minutes=26)
    assert ledger.settle(now=now)=={"settled":0,"ambiguous":2}
    assert ledger.report(asset="BTC",horizon=15,now=now)=={}
    # OHLC cannot establish which barrier hit first, but the observed CLOSE
    # still establishes a direction/size/timing target independently.
    task_scores=ledger.target_report(asset="BTC",horizon=15,now=now)
    assert len(task_scores)==2
    assert all(v["early_close_move"]["targets"]==1 for v in task_scores.values())
    with hot.connect() as conn:
        target_rows=conn.execute(
            """SELECT state,direction_positive,absolute_move_realized
               FROM rival_forward_path_targets"""
        ).fetchall()
        assert all(t["state"]=="settled" and t["absolute_move_realized"]==0
                   for t in target_rows)
        rows=conn.execute(
            "SELECT state,tp1_before_stop,tp2_before_stop FROM rival_forward_paths",
        ).fetchall()
        assert all(r["state"]=="ambiguous"
                   and r["tp1_before_stop"] is None
                   and r["tp2_before_stop"] is None for r in rows)


def test_path_sample_memo_carried_across_cycles_changes_nothing():
    """Reusing earlier cycles' per-bar samples gives the exact same priors."""
    from parlay_bot.perpetuals.path_forward import prune_path_memo

    history = _candles(count=430)
    for horizon in (15, 60):
        memo: dict = {}
        steps = horizon // 5
        for shift in range(steps + 2):  # every grid phase, then repeats
            window = history[shift:shift + 400]
            now = window[-1].observed_at + timedelta(seconds=5)
            before = len(memo)
            assert (empirical_paths(window, horizon=horizon, now=now, memo=memo)
                    == empirical_paths(window, horizon=horizon, now=now))
            prune_path_memo(memo, window)
            assert all(key[0] >= window[0].opened_at for key in memo)
            if shift >= steps:  # the grid has come round: nothing new to compute
                assert len(memo) <= before + 1


def test_direction_and_early_move_are_prospective_separate_heads(learning_store):
    market=PerpetualMarketStore(learning_store)
    market.initialize()
    ledger=ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles=_candles()
    last=candles[-1]
    issued=last.observed_at+timedelta(seconds=1)
    with learning_store.connect() as conn:
        assert ledger.issue(
            conn,candles=candles,asset="BTC",horizon=15,now=issued,
            issued_at=issued,
        )==2
        assert ledger.issue(
            conn,candles=candles,asset="BTC",horizon=15,now=issued,
            issued_at=issued,
        )==0
        frozen=conn.execute(
            """SELECT side,direction_probability,early_move_probability,
                      absolute_move_p50,model_version,close_target_training_sha256,state FROM rival_forward_path_targets
               ORDER BY side"""
        ).fetchall()
    assert len(frozen)==2
    assert all(row["state"]=="pending"
               and row["model_version"]==TARGET_VERSION
               and len(row["close_target_training_sha256"])==64
               and 0<row["direction_probability"]<1
               and 0<row["early_move_probability"]<1
               and row["absolute_move_p50"]>=0
               for row in frozen)
    # The bar already in progress when the worker issued is NOT a label.
    # Only new, source-matched, fully observed close values can grade.
    bars=[]
    for j in range(2,5):
        opened=last.opened_at+timedelta(minutes=5*j)
        price=last.close_price*1.02
        bars.append(MarketCandle(
            venue=last.venue,contract=last.contract,quote=last.quote,
            interval_minutes=5,opened_at=opened,
            closed_at=opened+timedelta(minutes=5,milliseconds=-1),
            observed_at=opened+timedelta(minutes=5,seconds=2),
            open_price=price,high_price=price+0.001,
            low_price=price-0.001,close_price=price,
            volume_base=10.0,trades=8,
        ))
    with learning_store.connect() as conn:
        for bar in bars:
            conn.execute("INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         _row(bar))
        conn.execute(
            """UPDATE perp_market_candles SET first_observed_at=?
               WHERE venue=? AND opened_at=?""",
            ((issued-timedelta(seconds=1)).isoformat(),last.venue,
             bars[0].opened_at.isoformat()),
        )
    now=bars[-1].observed_at+timedelta(minutes=1)
    assert ledger.settle(now=now)=={"settled":0,"ambiguous":0}
    with learning_store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_forward_path_targets WHERE state='settled'"
        ).fetchone()["n"]==0
        conn.execute(
            """UPDATE perp_market_candles SET first_observed_at=?
               WHERE venue=? AND opened_at=?""",
            (bars[0].observed_at.isoformat(),last.venue,
             bars[0].opened_at.isoformat()),
        )
    result=ledger.settle(now=now)
    assert result["settled"]+result["ambiguous"]==2
    with learning_store.connect() as conn:
        rows=conn.execute(
            """SELECT side,direction_positive,early_move_by_half,
                      first_close_hit_bars,absolute_move_realized,state
               FROM rival_forward_path_targets ORDER BY side"""
        ).fetchall()
    assert [r["side"] for r in rows]==["long","short"]
    assert [r["direction_positive"] for r in rows]==[1,0]
    assert all(r["early_move_by_half"]==1
               and r["first_close_hit_bars"]==1
               and r["absolute_move_realized"]==pytest.approx(math.log(1.02))
               and r["state"]=="settled" for r in rows)
    result=ledger.target_report(asset="BTC",horizon=15,now=now)
    assert len(result)==2
    assert all(v["early_close_move"]["targets"]==1
               and v["direction"]["targets"]==1
               and v["qualified_for_live_use"] is False
               for v in result.values())
