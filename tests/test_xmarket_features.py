from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.research.xmarket_features import (
    FEATURES,
    feature_names,
    frame_features,
    live_vector,
    market_values,
    sampled_bars,
)
from tests.xmarket_fixtures import frames_and_inputs, point


def candles(k, venue):
    return tuple(MarketCandle(venue=venue, contract="BTC", quote="USDT", interval_minutes=5,
        opened_at=at.to_pydatetime() - timedelta(minutes=5),
        closed_at=at.to_pydatetime() - timedelta(milliseconds=1), observed_at=at.to_pydatetime(),
        open_price=r.open, high_price=r.high, low_price=r.low, close_price=r.close,
        volume_base=r.volume, trades=3) for at, r in k.iterrows())


def test_all_named_inputs_are_eighteen_and_live_offline_match():
    frames, inputs = frames_and_inputs(n=450)
    k, f = frames["bitget|BTC"]
    venues = ("bitget", "okx")
    result = frame_features(k, f, asset="BTC", samples=inputs["samples"], frames=frames, venues=venues)
    vector, diagnostics = live_vector(candles(k, "bitget"), asset="BTC", samples=inputs["samples"],
        venue_candles={v: candles(frames[v + "|BTC"][0], v) for v in venues}, venues=venues,
        now=k.index[-1].to_pydatetime() + timedelta(seconds=2))
    assert len(vector) == len(FEATURES) == 18
    np.testing.assert_allclose(vector, result.iloc[-1].to_numpy(dtype=float), atol=1e-7, rtol=1e-6)
    np.testing.assert_allclose(vector[:10], f.iloc[-1].iloc[:10].to_numpy(dtype=float), atol=1e-7, rtol=1e-6)
    at = k.index[-1].to_pydatetime()
    pm = {r["at"]: r["price"] for r in inputs["samples"] if r["source"] == "polymarket"}
    assert vector[10] == pm[at.isoformat()]
    assert vector[11] == pm[at.isoformat()] - pm[(at - timedelta(minutes=15)).isoformat()]
    assert vector[12] == pm[at.isoformat()] - pm[(at - timedelta(minutes=60)).isoformat()]
    assert vector[13] == pytest.approx(vector[10] + .01)
    assert vector[14] == pytest.approx(.01)
    r1 = [frames[v + "|BTC"][1]["ret_1"].iloc[-1] for v in venues]
    r5 = [frames[v + "|BTC"][1]["ret_5"].iloc[-1] for v in venues]
    assert vector[15:] == pytest.approx((max(r1) - min(r1), sum(v > 0 for v in r1) / 2, max(r5) - min(r5)), abs=1e-7)
    assert diagnostics["pm_spread"] == pytest.approx(.02)
    assert diagnostics["kalshi_spread"] == pytest.approx(.02)
    assert diagnostics["native_horizon_minutes"] == 15
    assert not {"pm_spread", "kalshi_spread", "pm_vol_z"} & set(FEATURES)


@pytest.mark.parametrize("missing", ["polymarket", "kalshi"])
def test_missing_required_market_abstains(missing):
    frames, inputs = frames_and_inputs(n=425)
    k = frames["bitget|BTC"][0]
    assert live_vector(candles(k, "bitget"), asset="BTC",
        samples=[s for s in inputs["samples"] if s["source"] != missing],
        venue_candles={}, venues=()) is None


def test_fewer_than_two_venues_has_its_own_reduced_contract():
    assert len(feature_names(())) == 15
    with pytest.raises(ValueError):
        feature_names(("okx",))
    frames, inputs = frames_and_inputs(n=425)
    k = frames["bitget|BTC"][0]
    assert live_vector(candles(k, "bitget"), asset="BTC", samples=inputs["samples"],
        venue_candles={}, venues=("bitget", "okx")) is None
    assert len(live_vector(candles(k, "bitget"), asset="BTC", samples=inputs["samples"],
        venue_candles={}, venues=())[0]) == 15


def test_sampling_never_uses_future_or_carries_a_coarse_observation():
    at = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)
    p = point("polymarket", at, .7)
    bars = sampled_bars([p], asset="BTC", as_of=at)
    assert bars["polymarket"] == {}  # not received yet
    bars = sampled_bars([p], asset="BTC", as_of=at + timedelta(seconds=2))
    assert at in bars["polymarket"]
    assert at + timedelta(minutes=5) not in bars["polymarket"]
    assert market_values(bars, at) is None  # lag points and Kalshi are mandatory


def test_missing_offline_pinned_venue_does_not_change_input_meaning():
    frames, inputs = frames_and_inputs(n=425)
    k, f = frames["bitget|BTC"]
    with pytest.raises(ValueError, match="missing pinned"):
        frame_features(k, f, asset="BTC", samples=inputs["samples"],
                       frames={"bitget|BTC": (k, f)}, venues=("bitget", "okx"))


def test_native_fifteen_minute_fallback_does_not_fill_five_minute_rows():
    frames, inputs = frames_and_inputs(n=450)
    k, f = frames["bitget|BTC"]
    result = frame_features(k, f, asset="BTC", samples=inputs["samples"],
                            frames=frames, venues=(), pm_fidelity=15)
    assert result["pm_up"].notna().any()
    assert all(t.minute % 15 == 0 for t in result[result["pm_up"].notna()].index)
    assert live_vector(candles(k, "bitget"), asset="BTC", samples=inputs["samples"],
                       venue_candles={}, venues=(), pm_fidelity=15) is None
