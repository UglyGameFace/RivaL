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
        conn.execute(
            """UPDATE perp_market_candles SET first_observed_at=?
               WHERE venue='hyperliquid' AND opened_at=?""",
            (future[1].observed_at.isoformat(),future[1].opened_at.isoformat()),
        )
    assert ledger.settle(now=later)=={"settled":2,"ambiguous":0}
    assert ledger.settle(now=later)=={"settled":0,"ambiguous":0}
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
    with hot.connect() as conn:
        rows=conn.execute(
            "SELECT state,tp1_before_stop,tp2_before_stop FROM rival_forward_paths",
        ).fetchall()
        assert all(r["state"]=="ambiguous"
                   and r["tp1_before_stop"] is None
                   and r["tp2_before_stop"] is None for r in rows)
