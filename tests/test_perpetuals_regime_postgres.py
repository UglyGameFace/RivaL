"""PostgreSQL 16 stores prospective regime metadata durably and immutably."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.regime_evidence import VERSION
from parlay_bot.storage.hot import PostgresHotStore


def test_postgres_regime_provenance_matches_prospective_prediction():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres integration service not configured")
    store = PostgresHotStore(dsn)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    venue, contract, quote = "pg-regime-ci", "BTC-REGIME-CI", "USDT"
    now = datetime.now(UTC)
    ref = (now - timedelta(minutes=25)).replace(microsecond=0)
    target = ref + timedelta(minutes=5)
    issued = ref + timedelta(minutes=2)
    try:
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,
                     target_opened_at,probability_up,baseline_up,training_evaluated,
                     settled_at,outcome_up,realized_close,model_brier,baseline_brier
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (venue, contract, quote, "BTC", 5, MODEL,
                 ref.isoformat(), 100.0, issued.isoformat(),
                 target.isoformat(), 0.7, 0.5, 70,
                 (target + timedelta(minutes=6)).isoformat(),
                 1, 101.0, 0.09, 0.25),
            )
            conn.execute(
                """INSERT INTO perp_forward_regimes
                   (venue,contract,quote,horizon_minutes,model_version,
                    reference_opened_at,issued_at,method_version,regime,
                    five_min_volatility_pct,feature_sha256,weights_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (venue, contract, quote, 5, MODEL, ref.isoformat(),
                 issued.isoformat(), VERSION, "quiet", 0.02,
                 "a" * 64, "b" * 64),
            )
        restarted = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        sample = restarted.regime_summary(asset="BTC", horizon_minutes=5, now=now)
        assert sample["regimes"]["quiet"]["unique_targets"] >= 1
        assert sample["regimes"]["quiet"]["status"] == "insufficient_forward_evidence"
        assert sample["actual_execution_costs_measured"] is False
        assert sample["strategy_profitability_verified"] is False
        with store.connect() as conn:
            provenance = conn.execute(
                """SELECT feature_sha256,weights_sha256,issued_at
                   FROM perp_forward_regimes
                   WHERE venue=? AND contract=?""",
                (venue, contract),
            ).fetchone()
        assert provenance["weights_sha256"] == "b" * 64
        assert provenance["issued_at"] == issued.isoformat()
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_forward_regimes WHERE venue=? AND contract=?",
                (venue, contract),
            )
            conn.execute(
                "DELETE FROM perp_forward_audit WHERE venue=? AND contract=?",
                (venue, contract),
            )
        store.close()
