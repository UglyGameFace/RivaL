"""Prospective first-passage paper forecasts: SQLite/Postgres, real time fences."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.first_passage import trace_barriers
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.path_forward import (
    HORIZONS,
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
                      absolute_move_p50,model_version,state FROM rival_forward_path_targets
               ORDER BY side"""
        ).fetchall()
    assert len(frozen)==2
    assert all(row["state"]=="pending"
               and row["model_version"]==TARGET_VERSION
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
