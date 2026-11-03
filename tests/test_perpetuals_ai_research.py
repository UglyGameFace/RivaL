"""Cross-venue evidence and preserved historical accounting regression tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.history_inventory import history_inventory, six_months_prior
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def fixtures(market: PerpetualMarketStore, *, now: datetime, count: int = 289) -> None:
    period = timedelta(minutes=5)
    now = now.replace(second=0, microsecond=0)
    now = now.replace(minute=(now.minute // 5) * 5)
    for venue, contract, quote in (
        ("hyperliquid", "BTC", "USDC"),
        ("dydx", "BTC-USD", "USD"),
        ("bitget", "BTCUSDT", "USDT"),
    ):
        candles = []
        for i in range(count):
            opened = now - period * (count - i)
            base = 100 + i / 20
            candles.append(MarketCandle(
                venue, contract, quote, 5, opened,
                opened + period - timedelta(milliseconds=1), now,
                base, base + 3, base - 3, base + 1, 2.0, 15,
            ))
        assert market.ingest(MarketBatch((), tuple(candles), 5, now)).inserted_candles == count
        market.record_feed_health(
            venue=venue, asset="BTC", status="ok", attempted_at=now,
            new_candles=count, total_candles=count,
        )


@pytest.mark.asyncio
async def test_ai_evidence_consumes_all_healthy_venues_and_many_windows(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    clock = datetime.now(UTC).replace(second=0, microsecond=0)
    clock = clock.replace(minute=(clock.minute // 5) * 5)
    try:
        fixtures(market, now=clock)
        evidence, digest = build_evidence(
            market, asset="BTC",
            venues=("hyperliquid", "dydx", "bitget", "gate"),
            now=clock + timedelta(minutes=2),
        )
        assert len(evidence["sources"]) == 3
        assert {x["venue"] for x in evidence["sources"]} == {
            "hyperliquid", "dydx", "bitget",
        }
        assert evidence["unavailable_sources"] == [
            {"venue": "gate", "status": "never"}
        ]
        assert all(
            set(x["windows"]) == {"5m", "15m", "1h", "4h", "24h"}
            for x in evidence["sources"]
        )
        assert all(x["historical_windows"] == {} for x in evidence["sources"])
        assert evidence["horizon_minutes"] == 5
        assert len(digest) == 64
        assert digest == build_evidence(
            market, asset="BTC",
            venues=("hyperliquid", "dydx", "bitget", "gate"),
            now=clock + timedelta(minutes=3),
        )[1]
        with pytest.raises(InsufficientEvidence):
            build_evidence(market, asset="BTC",
                           venues=("hyperliquid", "dydx"), now=clock)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_persisted_quota_rejects_requests_after_limit_and_honors_cooldown(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    # Daily budget resets at midnight UTC; this test exercises SAME-DAY caps.
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    try:
        assert market.reserve_ai_request(
            model="gemini-test", now=now, daily_max=2, cooldown_seconds=900,
        )
        assert not market.reserve_ai_request(
            model="gemini-test", now=now + timedelta(seconds=2),
            daily_max=2, cooldown_seconds=900,
        )
        assert market.reserve_ai_request(
            model="gemini-test", now=now + timedelta(minutes=16),
            daily_max=2, cooldown_seconds=900,
        )
        assert not market.reserve_ai_request(
            model="gemini-test", now=now + timedelta(minutes=32),
            daily_max=2, cooldown_seconds=900,
        )
    finally:
        store.close()


def test_history_is_six_calendar_months_and_never_faked(tmp_path):
    assert six_months_prior(
        datetime(2026, 8, 31, tzinfo=UTC)
    ).date().isoformat() == "2026-02-28"
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    try:
        status = history_inventory(
            market, asset="BTC", venues=("hyperliquid", "dydx"),
            now=datetime(2026, 10, 28, tzinfo=UTC),
        )
        assert status["saved"] == 0
        assert status["expected"] > 100_000
        assert status["coverage_pct_ceiling"] == 0
        assert status["gap_audit"] == "not_yet_performed"
    finally:
        store.close()


def test_research_dashboard_does_not_claim_active_math_or_unconfigured_ai():
    view = ResearchDashboard(owner_id=15, market=None, ai=None, venues=())
    assert {x.custom_id for x in view.children} == {
        "rival:research:math", "rival:research:asset",
        "rival:research:refresh", "rival:research:accuracy",
        "rival:research:opportunities",
        "rival:research:section",
    }


def test_xrp_intelligence_reuses_persisted_model_and_history_audit(tmp_path):
    """XRP source history and math provenance enter the actual AI evidence digest."""
    from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle

    store = SQLiteHotStore(tmp_path / "xrp.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
    contracts = (
        ("bitget", "XRPUSDT", "USDT"),
        ("gate", "XRP_USDT", "USDT"),
        ("okx", "XRP-USDT-SWAP", "USDT"),
    )
    for venue, contract, quote in contracts:
        candles = tuple(
            MarketCandle(
                venue, contract, quote, 5,
                now - timedelta(minutes=5 * (13-i)),
                now - timedelta(minutes=5 * (12-i), milliseconds=1),
                now, 1.0, 1.2, .9, 1.1, 3.0, 20,
            ) for i in range(13)
        )
        market.ingest(MarketBatch((), candles, 5, now))
        market.record_feed_health(
            venue=venue, asset="XRP", status="ok", attempted_at=now,
            new_candles=13, total_candles=13,
        )

    class StoredModels:
        def summary(self, **scope):
            assert scope["asset"] == "XRP" and scope["horizon"] == 5
            assert len(scope["live_venues"]) == 3
            return {
                "slot_imported": 3, "slot_compatible": 2,
                "slot_passed": 1, "slot_live_source_matched": 1,
                "active_offline_model": False,
                "candidate": {"model_sha256": "a" * 64,
                              "holdout_brier": .248,
                              "holdout_accuracy": .51},
            }

    class StoredAudits:
        def latest(self, **scope):
            assert scope["asset"] == "XRP"
            return [{
                "venue": "bitget", "asset": "XRP", "audited_at": now.isoformat(),
                "window_start": (now - timedelta(days=180)).isoformat(),
                "window_end": now.isoformat(), "expected_bars": 51840,
                "valid_bars": 51840, "missing_bars": 0,
                "off_grid_bars": 0, "invalid_observations": 0,
                "status": "complete",
            }]

    try:
        market.research_candidate_registry = StoredModels()
        market.research_history_auditor = StoredAudits()
        class StoredForward:
            def active_learning_evidence(self, **scope):
                assert scope["asset"] == "XRP"
                return {
                    "status": "supported", "targets": 150, "days": 7,
                    "brier": .23, "baseline_brier": .25,
                }
        market.research_forward_auditor = StoredForward()
        evidence, digest = build_evidence(
            market, asset="XRP", venues=tuple(row[0] for row in contracts),
            now=now + timedelta(minutes=2),
        )
        assert evidence["mathematical_research"]["passed_retrospective_screen"] == 1
        assert evidence["mathematical_research"]["forward_edge_verified"] is False
        assert evidence["historical_quality"][0]["status"] == "complete"
        assert evidence["prospective_math"]["settled_targets"] == 150
        assert evidence["prospective_math"]["after_cost_edge_verified"] is False
        assert evidence["cold_archive_readback"] == "not_performed_by_live_research"
        from parlay_bot.perpetuals.evidence_policy import enforce
        reply = enforce(
            {"direction": "up", "confidence": "high"},
            evidence, asset="XRP", now=now + timedelta(minutes=2),
        )
        assert reply["direction"] == "uncertain"
        assert "1/3" in reply["thesis"]
        assert "1/1" in reply["thesis"]
        assert "0.230" in reply["thesis"]
        assert reply["informational_only"] is True
        market.research_candidate_registry = None
        evidence_without_models, digest_without_models = build_evidence(
            market, asset="XRP", venues=tuple(row[0] for row in contracts),
            now=now + timedelta(minutes=2),
        )
        assert evidence_without_models["mathematical_research"]["status"] == "not_configured"
        assert digest != digest_without_models
    finally:
        store.close()


def test_stale_history_audit_cannot_prove_six_month_completeness(tmp_path):
    from parlay_bot.perpetuals.analysis_evidence import _audited_history_evidence
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    market = PerpetualMarketStore(store)
    clock = datetime(2026, 11, 2, 16, tzinfo=UTC)

    class OldAudit:
        def latest(self, **_scope):
            return [{
                "venue": "okx", "asset": "BTC",
                "audited_at": (clock - timedelta(days=3)).isoformat(),
                "window_start": (clock - timedelta(days=180)).isoformat(),
                "window_end": clock.isoformat(), "expected_bars": 50000,
                "valid_bars": 50000, "missing_bars": 0,
                "off_grid_bars": 0, "invalid_observations": 0,
                "status": "complete",
            }]

    try:
        market.research_history_auditor = OldAudit()
        report = _audited_history_evidence(
            market, asset="BTC", venues=("okx",), clock=clock,
        )
        assert report[0]["status"] == "stale"
        assert report[0]["cold_drive_originals_reverified_this_request"] is False
    finally:
        store.close()

def test_real_empty_registry_proves_unknown_not_fake_historical_skill(tmp_path):
    from parlay_bot.perpetuals.analysis_evidence import _persisted_math_evidence
    from parlay_bot.research.candidate_registry import ResearchCandidateRegistry

    store = SQLiteHotStore(tmp_path / "no_models.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.research_candidate_registry = ResearchCandidateRegistry(store)
        result = _persisted_math_evidence(
            market, asset="BTC", venues=("binance",),
        )
        assert result["status"] == "persisted_registry"
        assert result["imported_this_asset_horizon"] == 0
        assert result["passed_retrospective_screen"] == 0
        assert result["selected_after_forward_comparison"] is False
        assert result["forward_edge_verified"] is False
    finally:
        store.close()
