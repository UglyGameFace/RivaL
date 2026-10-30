"""Autonomous dual-track issuance, settlement, gates, and analytics.

All network outcomes are mocked. PostgreSQL-specific coverage is separate.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import parlay_bot.perpetuals.autonomous as autonomous_module
from parlay_bot.perpetuals.analytics import _bucket, analytics_snapshot
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.gemini_research import AiResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def utc_bucket(now):
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)


class StubGemini:
    model = "gemini-test-flash"
    available = True

    def __init__(self):
        self.requests = 0

    async def ask(self, *, asset, venues):
        self.requests += 1
        assert asset in ("BTC", "ETH", "SOL")
        assert venues == ("hyperliquid", "dydx", "bitget")
        return AiResearch(
            asset=asset, model=self.model, generated_at=datetime.now(UTC),
            direction="up", confidence="low", thesis="Uncalibrated",
            risk="Unknown fills", source_count=3, evidence_sha256="a" * 64,
        )


def fake_evidence(market, *, asset, venues, now):
    target = utc_bucket(now)
    reference = target - timedelta(milliseconds=1)
    return ({
        "asset": asset,
        "sources": [{
            "venue": "hyperliquid", "contract": asset, "quote": "USDC",
            "close": 100.0, "last_candle_close": reference.isoformat(),
        }, {"venue": "dydx"}, {"venue": "bitget"}],
    }, "a" * 64)


@pytest.mark.asyncio
async def test_background_ai_prediction_is_unique_persisted_and_later_settled(
    tmp_path, monkeypatch,
):
    store = SQLiteHotStore(tmp_path / "autonomous.sqlite")
    market = PerpetualMarketStore(store)
    ai = StubGemini()
    monitor = AutonomousResearch(
        market, ai=ai, venues=("hyperliquid", "dydx", "bitget"),
    )
    monkeypatch.setattr(autonomous_module, "build_evidence", fake_evidence)
    now = datetime.now(UTC)
    try:
        assert await monitor.record_ai_forecast(asset="BTC", now=now)
        assert not await monitor.record_ai_forecast(asset="BTC", now=now)
        assert ai.requests == 1
        with store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM perp_auto_ai_forecasts WHERE asset='BTC'"
            ).fetchone()
            assert row is not None
            assert row["settled_at"] is None
            target = datetime.fromisoformat(row["target_opened_at"])
            issued = datetime.fromisoformat(row["issued_at"])
            assert issued < target + timedelta(minutes=5)
            assert row["direction"] == "up"
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_auto_ai_slots"
            ).fetchone()["n"] == 1
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", 5, target.isoformat(),
                 (target + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                 (target + timedelta(minutes=5, seconds=4)).isoformat(),
                 100, 103, 99, 102, 2, 12),
            )
        assert monitor.settle_ai(
            now=target + timedelta(minutes=6)
        ) == 1
        assert monitor.settle_ai(
            now=target + timedelta(minutes=6)
        ) == 0
        with store.connect() as conn:
            result = conn.execute(
                "SELECT * FROM perp_auto_ai_forecasts WHERE asset='BTC'"
            ).fetchone()
            assert result["outcome_up"] == 1
            assert result["correct"] == 1
        report = analytics_snapshot(
            market, asset="BTC", now=target + timedelta(minutes=6),
        )
        assert report["gemini"]["issued"] == 1
        assert report["gemini"]["settled"] == 1
        assert report["gemini"]["directional_accuracy"] == 1.
    finally:
        store.close()


@pytest.mark.asyncio
async def test_stale_source_cannot_claim_slot_or_predict(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    market = PerpetualMarketStore(store)
    ai = StubGemini()
    monitor = AutonomousResearch(
        market, ai=ai, venues=("hyperliquid", "dydx", "bitget"),
    )

    def stale(market, *, asset, venues, now):
        evidence, digest = fake_evidence(
            market, asset=asset, venues=venues, now=now,
        )
        evidence["sources"][0]["last_candle_close"] = (
            now - timedelta(minutes=30)
        ).isoformat()
        return evidence, digest

    monkeypatch.setattr(autonomous_module, "build_evidence", stale)
    try:
        assert not await monitor.record_ai_forecast(
            asset="BTC", now=datetime.now(UTC),
        )
        assert ai.requests == 0
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_auto_ai_slots"
            ).fetchone()["n"] == 0
    finally:
        store.close()


def test_autonomous_governance_counts_correlated_exchanges_once(tmp_path):
    store = SQLiteHotStore(tmp_path / "governance.sqlite")
    market = PerpetualMarketStore(store)
    supervisor = AutonomousResearch(market, ai=None, venues=())
    supervisor.initialize()
    now = datetime.now(UTC)
    # 105 distinct 5m targets on 7 UTC dates, two correlated venues each.
    # Fixed clock avoids local time-of-day/window-boundary flakes.
    now = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
    today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if today > now:
        today -= timedelta(days=1)
    try:
        with store.connect() as conn:
            for day in range(7):
                for i in range(15):
                    target = today - timedelta(days=6 - day, minutes=5 * i)
                    for venue in ("hyperliquid", "dydx"):
                        conn.execute(
                            """INSERT INTO perp_forward_audit
                               (venue,contract,quote,asset,horizon_minutes,
                                model_version,reference_opened_at,reference_close,
                                issued_at,target_opened_at,probability_up,
                                baseline_up,training_evaluated,settled_at,
                                outcome_up,realized_close,model_brier,
                                baseline_brier)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (venue, "BTC", "USDC", "BTC", 5, "prospective-logistic-persistent-v3",
                             (target - timedelta(minutes=5)).isoformat(),
                             100., (target - timedelta(minutes=2)).isoformat(),
                             target.isoformat(), .7, .5, 100,
                             (target + timedelta(minutes=6)).isoformat(),
                             1, 102., .09, .25),
                        )
        assert supervisor.review_model(now=now) >= 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_model_governance
                   WHERE asset='BTC' AND horizon_minutes=5
                   ORDER BY evaluated_at DESC LIMIT 1"""
            ).fetchone()
        assert row["unique_target_windows"] == 105
        assert row["calendar_days"] == 7
        assert row["status"] == "qualified_shadow"
        # Losing the rolling history automatically retracts qualification.
        supervisor.review_model(now=now + timedelta(days=8))
        with store.connect() as conn:
            latest = conn.execute(
                """SELECT status FROM perp_model_governance
                   WHERE asset='BTC' AND horizon_minutes=5
                   ORDER BY evaluated_at DESC LIMIT 1"""
            ).fetchone()
        assert latest["status"] == "insufficient"
    finally:
        store.close()


def test_zero_scores_are_not_claimed_as_predictive_skill(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    market = PerpetualMarketStore(store)
    try:
        report = analytics_snapshot(market, asset="SOL")
        assert report["math"][5]["last_7d"]["events"] == 0
        assert report["math"][5]["trend"] == "insufficient"
        assert report["math"][5]["last_7d"]["simulated_net_bps"] is None
        assert report["gemini"]["directional_accuracy"] is None
        assert report["model_governance"] == {}
    finally:
        store.close()


def test_calibration_uses_probability_bins_not_absolute_outcome_error():
    # At a 50% forecast, one up and one down are perfectly calibrated.
    values = [
        {"brier": .25, "baseline": .25, "probability": .5,
         "up_rate": float(y), "pred_count": 0., "gross_bps": None,
         "correct": None}
        for y in (0, 1)
    ]
    report = _bucket(values)
    assert report["events"] == 2
    assert report["ece"] == 0
    assert report["brier"] == .25
    assert report["decided_events"] == 0
    assert report["simulated_net_bps"] is None


@pytest.mark.asyncio
async def test_auto_ai_staggers_assets_to_respect_global_model_cooldown(
    tmp_path, monkeypatch,
):
    store = SQLiteHotStore(tmp_path / "slots.sqlite")
    market = PerpetualMarketStore(store)
    monitor = AutonomousResearch(
        market, ai=StubGemini(),
        venues=("hyperliquid", "dydx", "bitget"),
    )
    called = []

    async def fake_forecast(*, asset, now):
        called.append(asset)
        return True

    monkeypatch.setattr(monitor, "record_ai_forecast", fake_forecast)
    day = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    try:
        assert await monitor.run_auto_ai(now=day + timedelta(minutes=1)) == 1
        assert await monitor.run_auto_ai(now=day + timedelta(minutes=21)) == 1
        assert await monitor.run_auto_ai(now=day + timedelta(minutes=41)) == 1
        assert await monitor.run_auto_ai(now=day + timedelta(minutes=6)) == 0
        assert await monitor.run_auto_ai(now=day + timedelta(hours=1)) == 0
        assert called == ["BTC", "ETH", "SOL"]
    finally:
        store.close()
