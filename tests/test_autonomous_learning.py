"""Autonomous dual-track issuance, settlement, gates, and analytics.

All network outcomes are mocked. PostgreSQL-specific coverage is separate.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.ai_types import AiResearch
from parlay_bot.perpetuals.analytics import _bucket, analytics_snapshot
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def utc_bucket(now):
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)


class StubGemini:
    model = "gemini-test-flash"
    available = True

    def __init__(self):
        self.requests = 0

    async def ask(self, *, asset, venues, busy_retry=False):
        assert busy_retry is True  # background issuance survives a busy gateway
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
@pytest.mark.asyncio
def test_autonomous_governance_counts_correlated_exchanges_once(tmp_path):
    store = SQLiteHotStore(tmp_path / "governance.sqlite")
    market = PerpetualMarketStore(store)
    supervisor = AutonomousResearch(market, venues=())
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
