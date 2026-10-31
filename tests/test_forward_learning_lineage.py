"""Per-forecast learning provenance: historical artifacts are never assumed used."""

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def test_forward_learning_lineage_tracks_real_seed_and_unknown_legacy(tmp_path):
    hot = SQLiteHotStore(tmp_path / "lineage.sqlite")
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(hot))
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    monitor.initialize()
    empty = monitor.learning_lineage_summary(asset="BTC", horizon_minutes=5, now=now)
    assert empty["forecasts"] == 0
    assert empty["latest_lineage_verified"] is False
    assert empty["latest_training_labels"] is None

    # An old forecast without a lineage record must not be treated as
    # either a verified historical seed or a verified online-only model.
    legacy_at = now - timedelta(minutes=10)
    seeded_at = now - timedelta(minutes=5)
    with hot.connect() as conn:
        for stamp in (legacy_at, seeded_at):
            reference = (stamp - timedelta(minutes=5)).isoformat()
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("binance", "BTCUSDT", "USDT", "BTC", 5, MODEL,
                 reference, 65000., stamp.isoformat(),
                 (stamp + timedelta(minutes=5)).isoformat(), .55, .5, 100),
            )
        conn.execute(
            """INSERT INTO rival_forward_model_lineage
               (venue,contract,quote,horizon_minutes,model_version,
                reference_opened_at,issued_at,weights_sha256,feature_sha256,
                training_labels,historical_training_labels,
                online_training_updates,historical_seed_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("binance", "BTCUSDT", "USDT", 5, MODEL,
             (seeded_at - timedelta(minutes=5)).isoformat(), seeded_at.isoformat(),
             "a" * 64, "b" * 64, 520, 500, 20, "c" * 64),
        )

    result = monitor.learning_lineage_summary(asset="BTC", horizon_minutes=5, now=now)
    assert result["forecasts"] == 2
    assert result["lineage_proven"] == 1
    assert result["lineage_unknown"] == 1
    assert result["historically_seeded"] == 1
    assert result["latest_historical_seed_sha256"] == "c" * 64
    assert result["latest_training_labels"] == 520
    assert result["latest_historical_training_labels"] == 500
    assert result["latest_online_training_updates"] == 20
    assert result["forward_accuracy_verified_by_lineage"] is False
    with pytest.raises(ValueError):
        monitor.learning_lineage_summary(asset="DOGE", horizon_minutes=5, now=now)
    hot.close()

@pytest.mark.asyncio
async def test_results_page_shows_actual_training_provenance_without_claiming_accuracy(tmp_path):
    from parlay_bot.discord_app.research_dashboard import (
        ResearchDashboard, _compact_embed,
    )

    store = SQLiteHotStore(tmp_path / "results-lineage.sqlite")
    market = PerpetualMarketStore(store)
    view = ResearchDashboard(
        owner_id=24, market=market, ai=None,
        venues=("hyperliquid",), shadow_enabled=True,
    )
    try:
        view.screen = "accuracy"
        view.section = "lineage"
        view._rebuild()
        embed = await view.render()
        values = {field.name: field.value for field in embed.fields}
        proof = values["Model training actually used"]
        assert "No issued forecasts to audit" in proof
        assert "Training evidence is NOT a measured win rate" in proof
        compact = _compact_embed(embed, screen="accuracy", section="lineage")
        assert any(f.name == "Model training actually used" for f in compact.fields)
    finally:
        view.stop()
        store.close()
