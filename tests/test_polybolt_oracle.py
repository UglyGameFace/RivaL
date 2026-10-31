"""Official PolyBolt envelope and safe mathematical settlement research tests."""
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.polybolt_twap import CHANNEL, PolyBoltTWAP
from parlay_bot.perpetuals.polymarket_oracle import PolymarketOracleChallenger
from parlay_bot.research.contract_probability import (
    candidate_edge_after_cost,
    threshold_up_probability,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_polymarket_contract_research import notice


def stream():
    return PolyBoltTWAP(api_key="read-only-test", secret="secret", passphrase="passphrase")


def snapshot(now, *, source="chainlink", symbol="btcusd"):
    points = []
    for n in range(12):
        ts = now-timedelta(seconds=112-n*10)
        points.append({
            "timestamp": int(ts.timestamp()*1000),
            "full_accuracy_value": str(65000+n*3),
        })
    return {
        "v": 1, "channel": CHANNEL, "seq": 1, "snapshot": True,
        "payload": {"symbol": symbol, "source": source, "data": points},
    }


def validated_market(now):
    market = notice(now)
    return replace(
        market, price_to_beat=65000.,
        quote_at=now-timedelta(seconds=2),
        rules="BTC USD Chainlink 60-second TWAP at start and settlement",
    )


def test_polybolt_connect_requires_three_clob_credentials():
    assert not PolyBoltTWAP(api_key="abc", secret=None, passphrase="abc").configured
    assert stream().configured


def test_chainlink_twap_source_creates_real_threshold_features_and_math(tmp_path):
    now = datetime(2026, 10, 30, 12, 2, 30, tzinfo=UTC)
    feed = stream()
    feed.accept({"op": "authed", "rid": "a1"}, received_at=now)
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert feed.accept(snapshot(now), received_at=now) == 12
    market = validated_market(now)
    reference = feed.reference(market, now=now)
    assert reference is not None
    assert reference.asset == "BTC"
    assert reference.method == "chainlink-twap-60s"
    assert reference.price == pytest.approx(65033.)
    assert reference.horizon_volatility_pct > 0
    assert len(reference.source_receipt_sha256) == 64
    state = PolymarketOracleChallenger(SQLiteHotStore(tmp_path / "polybolt.sqlite"))
    try:
        from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
        PolymarketResearch(state.store).initialize()
        state.initialize()
        assert state.observe(market, reference, now=now) is None
        data = state.summary()
        assert data["observed_contracts"] == 1
        assert data["learned_contracts"] == 0
        with state.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_poly_oracle_math_forward"
            ).fetchone()
        assert row["oracle_receipt_sha256"] == reference.source_receipt_sha256
        assert 0 < row["probability_up"] < 1
        assert 0 < row["market_baseline"] < 1
        assert data["threshold_math_forward_resolved"] == 0
        assert data["threshold_math_brier"] is None
        assert data["promoted"] is False
    finally:
        state.store.close()


def test_feed_disagreement_stale_snapshots_and_sequence_gaps_cannot_train():
    now = datetime(2026, 10, 30, 12, 2, 30, tzinfo=UTC)
    market = validated_market(now)
    feed = stream()
    feed.accept({"op": "authed"}, received_at=now)
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert feed.accept(snapshot(now, source="pyth"), received_at=now) == 0
    assert feed.reference(market, now=now) is None
    feed.reset()
    feed.accept({"op": "authed"}, received_at=now)
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    feed.accept(snapshot(now), received_at=now)
    assert feed.reference(market, now=now) is not None
    assert feed.reference(market, now=now+timedelta(seconds=9)) is None
    bad_market = replace(market, resolution_source="https://www.binance.com")
    assert feed.reference(bad_market, now=now) is None
    assert feed.accept({
        "v":1, "channel":CHANNEL, "seq":4, "payload":{
            "symbol":"btcusd","source":"chainlink",
            "value":65040.,"timestamp":int(now.timestamp()*1000),
        },
    }, received_at=now) == 0
    assert feed.reference(market, now=now) is None
    assert feed.status(now=now)["last_error"] is not None


def test_unsupported_twap_payload_never_substitutes_unrelated_asset():
    now = datetime(2026, 10, 30, 12, 2, 30, tzinfo=UTC)
    feed = stream()
    feed.accept({"op": "authed"}, received_at=now)
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert feed.accept(snapshot(now, symbol="dogeusd"), received_at=now) == 0
    assert feed.reference(validated_market(now), now=now) is None
    feed.reset()
    feed.accept({"op": "authed"}, received_at=now)
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    altered = snapshot(now)
    altered["payload"]["data"][0]["full_accuracy_value"] = "nan"
    assert feed.accept(altered, received_at=now) == 11


def test_digital_math_is_monotonic_and_unverified_costs_are_unknown():
    base = {"price_to_beat": 65000., "remaining_volatility_pct": .8}
    above = threshold_up_probability(current_oracle_price=65100., **base)
    at = threshold_up_probability(current_oracle_price=65000., **base)
    below = threshold_up_probability(current_oracle_price=64900., **base)
    assert 0 < below < at < above < 1
    assert math.isfinite(above)
    assert threshold_up_probability(
        current_oracle_price=65100., price_to_beat=65000.,
        remaining_volatility_pct=1.6,
    ) < above
    with pytest.raises(ValueError):
        threshold_up_probability(
            current_oracle_price=1., price_to_beat=0.,
            remaining_volatility_pct=.8,
        )
    assert candidate_edge_after_cost(
        predicted_up=.7, yes_ask=.6, no_ask=.41,
        fee_per_share=None, slippage_per_share=None,
    )["status"] == "unknown_cost"
    costs = candidate_edge_after_cost(
        predicted_up=.7, yes_ask=.6, no_ask=.41,
        fee_per_share=.01, slippage_per_share=.005,
    )
    assert costs["yes_ev"] == pytest.approx(.085)
    assert costs["verified_edge"] is False


def test_polybolt_health_reports_real_twap_not_just_configuration():
    now = datetime(2026, 10, 30, 12, 2, 30, tzinfo=UTC)
    feed = stream()
    cold = feed.status(now=now)
    assert cold["configured"] is True
    assert cold["connected"] is False
    assert cold["valid_twap_live"] is False
    feed.accept({"op": "authed"}, received_at=now)
    assert feed.status(now=now)["authenticated"] is True
    assert feed.status(now=now)["valid_twap_live"] is False
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert feed.status(now=now)["connected"] is True
    assert feed.status(now=now)["valid_twap_live"] is False
    assert feed.accept(snapshot(now), received_at=now) == 12
    live = feed.status(now=now)
    assert live["valid_twap_live"] is True
    assert live["accepted_twap_samples"] == 12
    assert live["assets_fresh"] == ["BTC"]
    assert live["last_valid_twap_at"] == now.isoformat()
    assert feed.status(now=now+timedelta(seconds=9))["valid_twap_live"] is False
    feed.accept({"op": "error", "code": "unsupported-provider-message"}, received_at=now)
    assert feed.status(now=now)["last_error"] == "provider_error"
    assert feed.status(now=now)["connected"] is False
