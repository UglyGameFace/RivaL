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
        view.section = "progress"
        view._rebuild()
        progress = await view.render()
        assert len(progress.fields) <= 4  # live + three short progress fields
        names = [x.name for x in progress.fields]
        # One merged proof section: the per-family proof checks and the
        # per-regime splits sit on the same render, not in two sections.
        assert "Future model proof" in names
        assert "Prospective skill by market regime" in names
        assert "Trial detail (5-minute)" in names
        regime_value = next(
            x.value for x in progress.fields if "market regime" in x.name
        )
        assert "insufficient" in regime_value
        assert "quiet " in regime_value  # incumbent per-regime splits kept
        assert "regime_v1 5m" in regime_value  # family subsets kept
        assert "regime_v2" not in regime_value  # dead family removed
        # Deduplicated: the per-regime gains live in exactly one field.
        proof_value = next(
            x.value for x in progress.fields if x.name == "Future model proof"
        )
        assert "paired Brier gain" not in proof_value
        assert "paired Brier gain" in regime_value
        assert "quiet" not in proof_value
        detail_value = next(
            x.value for x in progress.fields if x.name == "Trial detail (5-minute)"
        )
        assert "No scored trial results yet" in detail_value
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
        # The standalone "regimes" section is gone — its evidence lives
        # in the merged "progress" section now.
        section = next(
            x for x in view.children
            if x.custom_id == "rival:research:section"
        )
        opts = {option.value for option in section.options}
        assert {"ledger", "progress", "costs"}.issubset(opts)
        assert "regimes" not in opts
    finally:
        view.stop()
        store.close()


def test_forward_score_counts_only_decided_predictions_in_accuracy_denominator(tmp_path):
    from datetime import UTC, datetime, timedelta

    from parlay_bot.perpetuals.forward_audit import MODEL

    store = SQLiteHotStore(tmp_path / "forward-score-denominator.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        monitor.initialize()
        stamp = datetime.now(UTC)
        with store.connect() as conn:
            for i, (probability, result) in enumerate(
                ((.9, 1), (.1, 1), (.5, 0))
            ):
                issued = stamp - timedelta(minutes=20 + i)
                conn.execute(
                    """INSERT INTO perp_forward_audit (
                         venue,contract,quote,asset,horizon_minutes,model_version,
                         reference_opened_at,reference_close,issued_at,
                         target_opened_at,probability_up,baseline_up,
                         training_evaluated,settled_at,outcome_up,realized_close,
                         model_brier,baseline_brier
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
                        issued.isoformat(), 100., issued.isoformat(),
                        (issued + timedelta(minutes=5)).isoformat(),
                        probability, .5, 100, stamp.isoformat(), result, 101.,
                        (probability - result) ** 2, (.5 - result) ** 2,
                    ),
                )
        score = monitor.summary(asset="BTC", now=stamp)["last_7d"][5]
        assert score["issued"] == 3
        assert score["settled"] == 3
        assert score["decided"] == 2
        assert score["correct"] == 1
        assert score["hit_rate"] == .5
        assert score["model_beats_baseline"] is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_trial_detail_field_carries_the_cut_metrics(tmp_path, monkeypatch):
    """Headline-only change: the metrics cut from the Performance page
    (vs guess, vs regime_v1, selection, coverage, availability,
    trending labels learned, weight drift, gain splits) render in
    Detailed Research's Trial detail field — one line per family,
    only the metrics each family carries."""
    import parlay_bot.discord_app.home as home_module
    from parlay_bot.perpetuals import regime_live_model, regime_model

    store = SQLiteHotStore(tmp_path / "trial-detail.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    pooled = {"issued": 12761, "graded": 12761, "decided": 12761, "hits": 6698,
              "windows": 1527, "model_brier": .249, "incumbent_brier": .251,
              "baseline_brier": .250}
    regime = {"issued": 935, "graded": 935, "decided": 935, "hits": 511,
              "windows": 133, "model_brier": .253, "incumbent_brier": .253,
              "baseline_brier": .250, "selection_effect": .001, "coverage": .06}
    live = {"issued": 434, "graded": 434, "decided": 434, "hits": 214,
            "windows": 56, "model_brier": .256, "incumbent_brier": .252,
            "baseline_brier": .250, "paired_gain_vs_regime_v1": -.004,
            "selection_effect": -.001, "coverage": .02,
            "availability_ratio": .45, "updates": 2292,
            "weight_drift_mean": .325, "paired_gain_last_7d": -.008,
            "paired_gain_days_8_30": None}
    monkeypatch.setattr(home_module, "_pooled_record", lambda *_: pooled)
    monkeypatch.setattr(regime_model, "trial_report", lambda *_, **__: regime)
    monkeypatch.setattr(
        regime_live_model, "trial_report",
        lambda *_, **k: live if k.get("family") == regime_live_model.BASE_FAMILY
        else {"issued": 0},
    )
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("hyperliquid", "dydx"), shadow_enabled=True,
    )
    try:
        view.screen = "accuracy"
        view.section = "progress"
        view._rebuild()
        progress = await view.render()
        fields = {x.name: x.value for x in progress.fields}
        detail = fields["Trial detail (5-minute)"]
        assert "**Pooled 6-year model:** vs guess 0.250" in detail
        assert ("**regime_v1:** vs guess 0.250 · selection +0.001 "
                "(harder subset) · coverage 6%") in detail
        assert ("**Live-learning:** vs guess 0.250 · vs regime_v1 -0.004 · "
                "selection -0.001 (easier subset) · coverage 2% · availability 45% · "
                "2,292 trending labels learned · weight drift 0.325 · "
                "gain last 7d -0.008") in detail
        assert "Live-learning fast" not in detail  # issued 0: no line
        assert "too early to judge" not in detail
        assert len(detail) <= 1024
    finally:
        view.stop()
        store.close()
