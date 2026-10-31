"""Causal oracle-context prediction research, never arbitrary exchange substitution."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.polymarket_oracle import (
    FEATURE_CONTRACT,
    MODEL,
    OracleReference,
    PolymarketOracleChallenger,
    oracle_features,
)
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_polymarket_contract_research import notice


def current_inputs(now):
    market = replace(
        notice(now), price_to_beat=65000.,
        rules="This market uses Chainlink BTC/USD 60-second TWAP at open and close.",
    )
    reference = OracleReference(
        asset="BTC", resolution_source=market.resolution_source,
        observed_at=now, price=65100.,
        horizon_volatility_pct=.35, source_receipt_sha256="a"*64,
        method="chainlink-twap-60s",
    )
    return market, reference


def test_oracle_model_needs_genuine_rule_matched_and_fresh_source():
    now = datetime(2026, 10, 30, 12, 0, 15, tzinfo=UTC)
    market, ref = current_inputs(now)
    features = oracle_features(market, ref, now=now)
    assert len(features) == 13
    assert features[10] > 0
    assert features[11] > 0
    assert features[12] == pytest.approx(.35)
    assert MODEL.endswith("v2") and FEATURE_CONTRACT.endswith("v2")
    for malformed in (
        replace(ref, asset="ETH"),
        replace(ref, resolution_source="bitget"),
        replace(ref, price=0),
        replace(ref, observed_at=now-timedelta(seconds=26)),
        replace(ref, method="binance-spot-hourly"),
        replace(ref, source_receipt_sha256="not-an-original-receipt"),
        replace(ref, horizon_volatility_pct=0),
    ):
        with pytest.raises(ValueError):
            oracle_features(market, malformed, now=now)
    with pytest.raises(ValueError):
        oracle_features(replace(market, price_to_beat=None), ref, now=now)
    with pytest.raises(ValueError):
        oracle_features(replace(market, rules=None), ref, now=now)


def test_oracle_challenger_requires_real_receipts_and_keeps_first_forward(tmp_path):
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "oracle.sqlite")
    live = PolymarketResearch(store)
    live.initialize()
    oracle = PolymarketOracleChallenger(store)
    market, ref = current_inputs(now)
    try:
        assert oracle.observe(market, ref, now=now) is None
        assert oracle.summary()["observed_contracts"] == 1
        assert oracle.summary()["learned_contracts"] == 0
        from parlay_bot.perpetuals.model_checkpoints import canonical
        inputs = canonical(oracle_features(market, ref, now=now))
        with store.connect() as conn:
            for i in range(100):
                expiry = now-timedelta(minutes=10+i*5)
                slug = f"oracle-training-{i}"
                condition = f"0x{i+1:064x}"
                conn.execute(
                    """INSERT INTO rival_poly_oracle_observations
                       (slug,condition_id,asset,horizon,resolution_source,
                        expires_at,observed_at,features_json,
                        evidence_sha256,evidence_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (slug,condition,"BTC",5,market.resolution_source,
                     expiry.isoformat(),
                     (expiry-timedelta(minutes=1)).isoformat(),
                     inputs,"b"*64,"{}"),
                )
                conn.execute(
                    """INSERT INTO rival_polymarket_ai_outcomes
                       (slug,condition_id,direction,resolved_at,
                        observed_at,receipt_sha256,receipt_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (slug,condition,"UP" if i%2 else "DOWN",
                     (expiry+timedelta(seconds=1)).isoformat(),
                     (expiry+timedelta(seconds=2)).isoformat(),
                     "c"*64,"{}"),
                )
        result = oracle.observe(market, ref, now=now)
        assert result is not None and result["research_only"] is True
        assert result["training_labels"] == 100
        first = oracle.summary()
        assert first["learned_contracts"] == 100
        assert first["forward_resolved"] == 0
        assert first["promoted"] is False
        assert first["model_brier"] is None
        restart = PolymarketOracleChallenger(store)
        restart.observe(
            replace(market, up_ask=.61, quote_at=now),
            ref, now=now,
        )
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_poly_oracle_forward"
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT updates FROM rival_poly_oracle_weights"
            ).fetchone()["updates"] == 100
    finally:
        store.close()


def test_live_polymarket_study_does_not_invent_oracle_reference(tmp_path):
    store = SQLiteHotStore(tmp_path / "no-oracle.sqlite")
    service = PolymarketResearch(store)
    now = datetime.now(UTC)
    market, ref = current_inputs(now)
    try:
        service.study(replace(market, oracle_reference=None))
        assert service.oracle_challenger.summary()["observed_contracts"] == 0
        service.study(replace(market, oracle_reference=replace(ref, asset="ETH")))
        assert service.oracle_challenger.summary()["observed_contracts"] == 0
        service.study(replace(market, oracle_reference=ref))
        assert service.oracle_challenger.summary()["observed_contracts"] == 1
    finally:
        store.close()
