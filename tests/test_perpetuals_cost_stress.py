"""No claimed trading edge from baseline skill before fees, funding and slip."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.analytics import analytics_snapshot
from parlay_bot.perpetuals.cost_stress import (
    CostScenario,
    score_cost_stress,
)
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def targets(
    *, days: int = 8, each: int = 20, gross_bps: float = 65.0,
    venues: int = 1,
) -> list[dict]:
    start = datetime(2026, 10, 19, tzinfo=UTC)
    return [
        {
            "target_opened_at": (start + timedelta(days=day, minutes=5 * i)).isoformat(),
            "pred_count": 1.0, "gross_bps": gross_bps,
        }
        for day in range(days)
        for i in range(each)
        for _ in range(venues)
    ]


def test_cost_stress_uses_one_unique_utc_target_not_correlated_venues():
    unique = score_cost_stress(targets())
    correlated = score_cost_stress(targets(venues=10))
    assert unique == correlated
    assert unique["unique_decided_targets"] == 160
    assert unique["utc_days"] == 8
    low = unique["scenarios"]["low_assumption"]
    medium = unique["scenarios"]["moderate_stress"]
    severe = unique["scenarios"]["severe_stress"]
    assert low["assumed_total_bps"] == 16
    assert medium["assumed_total_bps"] == 40
    assert severe["assumed_total_bps"] == 80
    assert low["mean_net_bps"] == 49
    assert medium["mean_net_bps"] == 25
    assert severe["mean_net_bps"] == -15
    assert low["status"] == "hypothetical_stress_pass_not_approved"
    assert medium["status"] == "hypothetical_stress_pass_not_approved"
    assert severe["status"] == "fails_cost_stress"
    assert unique["funding_measured"] is False
    assert unique["actual_fills_measured"] is False
    assert unique["live_trading_approved"] is False


def test_strong_daily_net_not_enough_without_many_days():
    few_days = score_cost_stress(targets(days=2, each=60, gross_bps=500))
    assert few_days["unique_decided_targets"] >= 100
    assert few_days["utc_days"] == 2
    assert all(x["status"] == "insufficient_forward_outcomes"
               for x in few_days["scenarios"].values())


def test_adverse_day_or_sparse_signal_never_qualifies_cost_stress():
    base = targets(gross_bps=90)
    for row in base:
        if row["target_opened_at"].startswith("2026-10-20") or row[
            "target_opened_at"
        ].startswith("2026-10-22"):
            row["gross_bps"] = -1200
    stress = score_cost_stress(base)
    assert stress["scenarios"]["low_assumption"]["status"] == "fails_cost_stress"
    for row in base:
        row["pred_count"] = 0
    abstaining = score_cost_stress(base)
    assert abstaining["unique_decided_targets"] == 0
    assert abstaining["scenarios"]["moderate_stress"]["mean_net_bps"] is None


def test_malformed_cost_inputs_or_target_times_rejected():
    with pytest.raises(ValueError):
        score_cost_stress(targets(), scenarios=(
            CostScenario("bad", 10, -1, 3, 1),
        ))
    with pytest.raises(ValueError):
        score_cost_stress([{
            "target_opened_at": "2026-10-28T01:00:00",
            "pred_count": 1, "gross_bps": 100,
        }])
    with pytest.raises(ValueError):
        score_cost_stress([{
            "target_opened_at": "2026-10-28T01:00:00+00:00",
            "pred_count": 1, "gross_bps": float("inf"),
        }])


def test_cost_stress_is_reported_with_baseline_adjusted_accuracy(tmp_path):
    store = SQLiteHotStore(tmp_path / "cost.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    now = datetime(2026, 10, 28, 18, tzinfo=UTC)
    try:
        with store.connect() as conn:
            for i in range(105):
                target = now - timedelta(days=5, minutes=5 * i)
                ref = target - timedelta(minutes=5)
                for venue, contract in (
                    ("hyperliquid", "BTC"), ("dydx", "BTC-USD"),
                ):
                    conn.execute(
                        """INSERT INTO perp_forward_audit
                           (venue,contract,quote,asset,horizon_minutes,
                            model_version,reference_opened_at,reference_close,
                            issued_at,target_opened_at,probability_up,
                            baseline_up,training_evaluated,settled_at,
                            outcome_up,realized_close,model_brier,
                            baseline_brier)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            venue, contract,
                            "USDC" if venue == "hyperliquid" else "USD",
                            "BTC", 5, MODEL, ref.isoformat(), 100.,
                            (target - timedelta(minutes=1)).isoformat(),
                            target.isoformat(), 0.7, 0.5, 100,
                            (target + timedelta(minutes=5)).isoformat(),
                            1, 100.7, .09, .25,
                        ),
                    )
        report = analytics_snapshot(market, asset="BTC", now=now)
        scoring = report["math"][5]["cost_stress_7d"]
        assert scoring["unique_decided_targets"] == 105
        assert scoring["utc_days"] == 1
        assert scoring["scenarios"]["severe_stress"]["status"] == (
            "insufficient_forward_outcomes"
        )
        assert report["backtested_pnl"] is False
        assert report["hypothetical_funding_stressed"] is True
    finally:
        store.close()
