"""Fair unattended 21-day after-cost proof scheduling, not paper fills."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.family_quotes import supports_quote_source
from parlay_bot.perpetuals.forward_audit import family_proof_review_slot, select_family_proof_scope


def _streams():
    return [
        *[("hyperliquid", coin, "USDC") for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("binance", coin + "USDT", "USDT")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("okx", coin + "-USDT-SWAP", "USDT")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("bitget", coin + "USDT", "USDT")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("gate", coin + "_USDT", "USDT")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("deribit", coin + "_USDC-PERPETUAL", "USDC")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("lighter", coin, "USD")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
    ]


def test_all_seven_sources_and_horizons_are_reviewed_before_evidence_stales():
    entries = _streams()
    start = datetime(2026,11,16,tzinfo=UTC)
    # Actual five-minute driver cadence, with one review on each 20m
    # boundary. No extra reviews are hidden in an aggregate/scope count.
    slots = [family_proof_review_slot(start+timedelta(minutes=5*i)) for i in range(28*12)]
    slots = [slot for slot in slots if slot is not None]
    seen = [select_family_proof_scope(entries, hour_slot=slot) for slot in slots]
    assert len(slots) == len(set(seen)) == 84
    assert all(s is not None and s[3] in (5, 15, 60) for s in seen)
    assert {s[:3] for s in seen} == set(entries)
    assert [select_family_proof_scope(entries, hour_slot=slot+84) for slot in slots] == seen
    assert [select_family_proof_scope(list(reversed(entries)), hour_slot=slot)
            for slot in slots] == seen
    assert all(supports_quote_source(s[0], s[2]) for s in seen)


def test_missing_unsupported_and_wrong_quote_are_deferred_without_fake_proof():
    assert select_family_proof_scope([], hour_slot=0) is None
    # Phemex now has a book adapter, so it provides source-matched bid/ask
    # snapshots (same as KuCoin) and is no longer deferred here.
    for venue in ("dydx","kucoin"):
        assert select_family_proof_scope([(venue,"BTCUSDT","USDT")], hour_slot=18) is None
    assert supports_quote_source("okx", "USDT")
    assert supports_quote_source("bitget", "USDT")
    assert not supports_quote_source("hyperliquid", "USDT")
    assert not supports_quote_source("binance", "USDC")
    assert select_family_proof_scope(
        [("binance", "BTCUSDT", "USDC")], hour_slot=8,
    ) is None
    assert select_family_proof_scope(
        [("gate", "BTCUSDT", "USDT"), ("deribit", "BTC-PERPETUAL", "USDC")], hour_slot=8,
    ) is None


def test_a_single_quoted_market_gets_all_horizons_and_restart_keeps_same_slot():
    entries = [("hyperliquid", "BTC", "USDC")]
    assert [select_family_proof_scope(entries, hour_slot=h)[-1]
            for h in range(3)] == [5, 15, 60]
    assert [select_family_proof_scope(entries, hour_slot=h)[-1]
            for h in range(3, 6)] == [5, 15, 60]
    now = datetime(2026,11,16,1,20,1,tzinfo=UTC)
    assert family_proof_review_slot(now) == family_proof_review_slot(now+timedelta(minutes=4))
    for minute in (5,10,15,25,30,35,45,50,55):
        assert family_proof_review_slot(now.replace(minute=minute)) is None


@pytest.mark.parametrize("hour", [-1, -12])
def test_reject_invalid_hour_slot(hour):
    with pytest.raises(ValueError, match="slot"):
        select_family_proof_scope(_streams(), hour_slot=hour)
