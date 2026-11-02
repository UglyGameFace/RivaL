"""Independent numerical oracles for RivaL math and venue-weighted forward scores."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.research.learning import _metrics
from parlay_bot.storage.hot import SQLiteHotStore


def test_offline_probability_metrics_match_hand_calculated_oracle():
    rows = [
        (0.8, 1, "2026-10-01"),
        (0.2, 0, "2026-10-01"),
        (0.6, 0, "2026-10-02"),
        (0.4, 1, "2026-10-02"),
    ]
    result = _metrics(rows, 0.5)
    assert result["count"] == 4
    assert result["days"] == 2
    assert result["brier"] == pytest.approx(0.2)
    assert result["baseline_brier"] == pytest.approx(0.25)
    assert result["brier_gain"] == pytest.approx(0.05)
    assert result["accuracy"] == pytest.approx(0.5)
    assert result["ece_10"] == pytest.approx(0.4)
    expected_logloss = -(math.log(.8) + math.log(.8) +
                         math.log(.4) + math.log(.4)) / 4
    assert result["log_loss"] == pytest.approx(expected_logloss, abs=1e-8)


def test_independently_settled_venue_disagreements_are_scored_without_duplication(tmp_path):
    store = SQLiteHotStore(tmp_path / "unbiased-forward.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    now = datetime(2026, 11, 2, 17, tzinfo=UTC)
    try:
        with store.connect() as conn:
            for day in range(7):
                target = now - timedelta(days=7 - day, hours=3)
                issued = target - timedelta(minutes=2)
                reference = target - timedelta(minutes=5)
                settled = target + timedelta(minutes=6)
                # Two OKX contracts must carry the weight of ONE venue.
                # Gate has an opposite, genuinely settled direction.
                for venue, contract, y, p in (
                    ("okx", "BTC-USDT-SWAP", 1, .9),
                    ("okx", "BTC-USDC-SWAP", 1, .9),
                    ("gate", "BTC_USDT", 0, .8),
                ):
                    quote = "USDC" if "USDC" in contract else "USDT"
                    identity = (venue, contract, quote, 5, MODEL)
                    conn.execute(
                        """INSERT INTO perp_forward_audit
                           (venue,contract,quote,asset,horizon_minutes,model_version,
                            reference_opened_at,reference_close,issued_at,
                            target_opened_at,probability_up,baseline_up,
                            training_evaluated,settled_at,outcome_up,realized_close,
                            model_brier,baseline_brier)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (*identity[:3], "BTC", identity[3], identity[4],
                         reference.isoformat(), 100., issued.isoformat(),
                         target.isoformat(), p, .5, 100, settled.isoformat(),
                         y, 101. if y else 99., (p - y) ** 2, .25),
                    )
                    conn.execute(
                        """INSERT INTO rival_forward_model_lineage
                           (venue,contract,quote,horizon_minutes,model_version,
                            reference_opened_at,issued_at,weights_sha256,
                            feature_sha256,training_labels,
                            historical_training_labels,online_training_updates,
                            historical_seed_sha256)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (*identity, reference.isoformat(), issued.isoformat(),
                         "a" * 64, "b" * 64, 100, 0, 30, None),
                    )
        score = monitor.active_learning_evidence(asset="BTC", horizon_minutes=5, now=now)
        assert score["targets"] == 7
        assert score["days"] == 7
        assert score["brier"] == pytest.approx(.325)
        assert score["baseline_brier"] == pytest.approx(.25)
        assert score["base_rate_gain"] == pytest.approx(-.075)
        assert score["status"] == "insufficient"
        assert score["frozen_weights_improvement_proven"] is False
    finally:
        store.close()
