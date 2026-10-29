"""Verify the Accuracy menu distinguishes immutable forward forecasts from profit."""
from __future__ import annotations

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.mark.asyncio
async def test_three_private_proof_pages_are_compact_and_query_real_store(tmp_path):
    store = SQLiteHotStore(tmp_path / "proof-ui.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("hyperliquid", "dydx"), shadow_enabled=True,
    )
    try:
        view.screen = "accuracy"
        view.section = "ledger"
        view._rebuild()
        ledger = await view.render()
        assert len(ledger.fields) <= 3
        assert any("Prediction ledger" in x.name for x in ledger.fields)
        assert "No pre-outcome" in next(
            x.value for x in ledger.fields if "Prediction ledger" in x.name
        )
        view.section = "regimes"
        view._rebuild()
        regime = await view.render()
        assert len(regime.fields) <= 3
        assert any("market regime" in x.name for x in regime.fields)
        assert "insufficient" in next(
            x.value for x in regime.fields if "market regime" in x.name
        )
        view.section = "costs"
        view._rebuild()
        costs = await view.render()
        assert len(costs.fields) <= 3
        proof = next(x.value for x in costs.fields
                     if x.name == "Execution cost verification")
        assert "NOT VERIFIED" in proof
        assert "not measured" in proof.lower() or "NOT measured" in proof
        assert "No live orders" in proof
        # All drilldowns are discoverable using the same Discord selector.
        section = next(
            x for x in view.children
            if x.custom_id == "rival:research:section"
        )
        opts = {option.value for option in section.options}
        assert {"ledger", "regimes", "costs"}.issubset(opts)
    finally:
        view.stop()
        store.close()
