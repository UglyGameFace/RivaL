"""Read-only production canary uses host evidence, not historical claims."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
from tests.test_portable_boosted_forward import _bytes, _document


def test_boosted_production_canary_fails_closed_before_delivery(learning_store):
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(learning_store))
    now = datetime(2026, 10, 31, 15, tzinfo=UTC)
    evidence = monitor.boosted_production_evidence(asset="BTC", now=now)
    assert evidence["portable_imported"] == 0
    assert evidence["portable_screened"] == 0
    assert evidence["issued"] == evidence["settled"] == evidence["quote_exits"] == 0
    assert evidence["fresh_paper_promotions"] == []
    assert "No screened portable boosted artifact" in evidence["status_reason"]
    assert evidence["actual_fills_verified"] is False
    assert evidence["actual_fees_funding_slippage_verified"] is False
    with pytest.raises(ValueError):
        monitor.boosted_production_evidence(asset="DOGE", now=now)


def test_boosted_production_canary_separates_import_issuance_settlement_and_quotes(
    learning_store,
):
    store = learning_store
    now = datetime(2026, 10, 31, 15, tzinfo=UTC)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    document = _document()
    candidate = ResearchCandidateRegistry(store).import_verified(
        "evidenceCanary1", _bytes(document), now=now-timedelta(days=2),
    )
    imported = monitor.boosted_production_evidence(asset="BTC", now=now)
    assert imported["portable_imported"] == imported["portable_screened"] == 1
    assert imported["issued"] == 0
    assert "await source-matched genuine future" in imported["status_reason"]
    assert monitor.boosted_production_evidence(asset="ETH", now=now)["portable_imported"] == 0

    family = "boosted_lightgbm_" + candidate["model_sha256"][:12]
    issued = now-timedelta(minutes=15)
    target = issued+timedelta(minutes=5)
    reference = issued-timedelta(minutes=5)
    settled = target+timedelta(minutes=6)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts (
               venue,contract,quote,asset,horizon_minutes,family,
               reference_opened_at,issued_at,target_opened_at,
               probability_up,incumbent_up,baseline_up,
               training_count,training_sha256,settled_at,outcome_up,
               model_brier,incumbent_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("binance","BTCUSDT","USDT","BTC",5,family,
             reference.isoformat(),issued.isoformat(),target.isoformat(),
             .8,.6,.5,0,candidate["model_sha256"],settled.isoformat(),1,
             .04,.16,.25),
        )
    future = monitor.boosted_production_evidence(asset="BTC", now=now)
    assert future["issued"] == future["settled"] == 1
    assert future["quote_exits"] == 0
    assert future["scopes_count"] == 1
    assert future["scopes"][0]["venue"] == "binance"
    assert "matched entry and exit book quotes missing" in future["status_reason"]
    assert not future["fresh_paper_promotions"]

    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_quote_shadow (
               venue,contract,quote,asset,horizon_minutes,family,
               reference_opened_at,issued_at,target_close_at,
               training_sha256,challenger_up,incumbent_up,
               challenger_side,incumbent_side,entry_at,entry_bid,
               entry_ask,quantity,state,exit_at,exit_bid,exit_ask,
               challenger_gross_bps,incumbent_gross_bps)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("binance","BTCUSDT","USDT","BTC",5,family,
             reference.isoformat(),issued.isoformat(),
             (target+timedelta(minutes=5)).isoformat(),
             candidate["model_sha256"],.8,.6,"LONG","LONG",issued.isoformat(),
             100.,100.1,.25,"EXIT_QUOTED",settled.isoformat(),
             101.,101.1,90.,80.),
        )
    quoted = monitor.boosted_production_evidence(asset="BTC", now=now)
    assert quoted["quote_entries"] == quoted["quote_exits"] == 1
    assert "21-day severe after-cost paper proof not qualified" in quoted["status_reason"]
    assert quoted["fresh_paper_promotions"] == []
    assert quoted["actual_fills_verified"] is False


@pytest.mark.asyncio
async def test_lineage_page_displays_real_boosted_delivery_proof_without_new_menu(tmp_path):
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "production-evidence.sqlite")
    market = PerpetualMarketStore(store)
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("binance",), shadow_enabled=True,
    )
    try:
        view.screen = "accuracy"
        view.section = "lineage"
        view._rebuild()
        embed = await view.render()
        fields = {field.name: str(field.value) for field in embed.fields}
        assert "Boosted model production proof" in fields
        proof = fields["Boosted model production proof"]
        assert "**Imported:** 0" in proof
        assert "No screened portable boosted artifact" in proof
        assert "NOT VERIFIED" in proof
        assert len(embed.fields) <= 3
    finally:
        view.stop()
        store.close()
