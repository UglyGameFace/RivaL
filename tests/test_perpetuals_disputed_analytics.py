"""Forward accuracy must never turn conflicting venue labels into fake accuracy."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.analytics import analytics_snapshot
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def insert_forecast(
    store, *, when: datetime, venue: str, label: int, p: float,
    horizon: int = 5, asset: str = "BTC",
) -> None:
    contract, quote = (
        ("BTC", "USDC") if venue == "hyperliquid" else
        ("BTC-USD", "USD") if venue == "dydx" else
        ("BTCUSDT", "USDT")
    )
    before = when - timedelta(minutes=horizon)
    base = 100.
    result = 101. if label else 99.
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit (
               venue,contract,quote,asset,horizon_minutes,model_version,
               reference_opened_at,reference_close,issued_at,target_opened_at,
               probability_up,baseline_up,training_evaluated,settled_at,
               outcome_up,realized_close,model_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, contract, quote, asset, horizon, MODEL,
             before.isoformat(), base,
             (before + timedelta(minutes=1)).isoformat(), when.isoformat(),
             p, .5, 100,
             (when + timedelta(minutes=5)).isoformat(),
             label, result, (p - label) ** 2, (.5 - label) ** 2),
        )


def test_disputed_target_is_withheld_from_all_accuracy_metrics(tmp_path):
    store = SQLiteHotStore(tmp_path / "dispute.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    now = datetime(2026, 10, 28, 18, tzinfo=UTC)
    try:
        same_time = now - timedelta(days=2)
        agreed = now - timedelta(days=1)
        insert_forecast(store, when=same_time, venue="hyperliquid", label=1, p=.85)
        insert_forecast(store, when=same_time, venue="dydx", label=0, p=.05)
        insert_forecast(store, when=agreed, venue="hyperliquid", label=1, p=.70)
        insert_forecast(store, when=agreed, venue="dydx", label=1, p=.60)
        stats = analytics_snapshot(market, asset="BTC", now=now)["math"][5]
        assert stats["disputed_targets_7d"] == 1
        assert stats["last_7d"]["events"] == 1
        assert stats["forward_reliability_7d"]["unique_targets"] == 1
        assert stats["cost_stress_7d"]["unique_decided_targets"] == 1
        assert stats["last_7d"]["decided_events"] == 1
        assert stats["last_7d"]["hit_rate"] == 1.
        assert stats["target_outcome_policy"] == "exclude_disagreeing_exchange_labels"
        assert stats["trend"] == "insufficient"
        assert stats["forward_reliability_7d"]["status"] == (
            "insufficient_independent_forward_history"
        )
    finally:
        store.close()


def test_average_venue_forecasts_abstain_instead_of_inventing_hit_rate(tmp_path):
    store = SQLiteHotStore(tmp_path / "neutral.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    now = datetime(2026, 10, 28, 18, tzinfo=UTC)
    try:
        when = now - timedelta(days=2)
        insert_forecast(store, when=when, venue="hyperliquid", label=1, p=.8)
        insert_forecast(store, when=when, venue="dydx", label=1, p=.2)
        stats = analytics_snapshot(market, asset="BTC", now=now)["math"][5]
        assert stats["last_7d"]["events"] == 1
        assert stats["last_7d"]["decided_events"] == 0
        assert stats["last_7d"]["hit_rate"] is None
        assert stats["last_7d"]["simulated_net_bps"] is None
        assert stats["cost_stress_7d"]["unique_decided_targets"] == 0
        assert stats["forward_reliability_7d"]["unique_targets"] == 1
        assert stats["forward_reliability_7d"]["bins"][0]["average_probability"] == .5
    finally:
        store.close()


def test_disputed_prior_targets_cannot_make_accuracy_trend_improve(tmp_path):
    store = SQLiteHotStore(tmp_path / "trend_dispute.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    now = datetime(2026, 10, 28, 18, tzinfo=UTC)
    try:
        for index in range(110):
            when = now - timedelta(days=11, minutes=index * 5)
            insert_forecast(store, when=when, venue="hyperliquid", label=1, p=.9)
            insert_forecast(store, when=when, venue="dydx", label=0, p=.1)
        for index in range(110):
            when = now - timedelta(days=3, minutes=index * 5)
            insert_forecast(store, when=when, venue="hyperliquid", label=1, p=.7)
            insert_forecast(store, when=when, venue="dydx", label=1, p=.7)
        report = analytics_snapshot(market, asset="BTC", now=now)["math"][5]
        assert report["disputed_targets_prior_7d"] == 110
        assert report["prior_7d"]["events"] == 0
        assert report["last_7d"]["events"] == 110
        assert report["trend"] == "insufficient"
        assert report["prior_7d_skill"] is None
    finally:
        store.close()
