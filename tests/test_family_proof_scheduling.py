"""Fair unattended 21-day after-cost proof scheduling, not paper fills."""
from __future__ import annotations

import pytest

from parlay_bot.perpetuals.forward_audit import select_family_proof_scope
from parlay_bot.perpetuals.family_quotes import supports_quote_source


def _streams():
    return [
        *[("hyperliquid", coin, "USDC") for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[("binance", coin + "USDT", "USDT")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
        *[(venue, coin + "-USDT-SWAP", "USDT")
          for venue in ("okx", "gate", "bitget")
          for coin in ("BTC", "ETH", "SOL", "XRP")],
    ]


def test_every_quote_supported_market_and_horizon_is_reviewed_within_a_day():
    entries = _streams()
    seen = [select_family_proof_scope(entries, hour_slot=hour)
            for hour in range(24)]
    assert len(set(seen)) == 24
    assert all(s is not None and s[3] in (5, 15, 60) for s in seen)
    assert {s[:3] for s in seen} == set(entries[:8])
    assert [select_family_proof_scope(entries, hour_slot=h)
            for h in range(24, 48)] == seen
    assert [select_family_proof_scope(list(reversed(entries)), hour_slot=h)
            for h in range(24)] == seen
    assert all(supports_quote_source(s[0], s[2]) for s in seen)


def test_missing_unsupported_and_wrong_quote_are_deferred_without_fake_proof():
    assert select_family_proof_scope([], hour_slot=0) is None
    assert select_family_proof_scope(_streams()[8:], hour_slot=18) is None
    assert not supports_quote_source("okx", "USDT")
    assert not supports_quote_source("hyperliquid", "USDT")
    assert not supports_quote_source("binance", "USDC")
    assert select_family_proof_scope(
        [("binance", "BTCUSDT", "USDC")], hour_slot=8,
    ) is None


def test_a_single_new_quoted_market_gets_all_three_proofs_every_three_hours():
    entries = [("hyperliquid", "BTC", "USDC")]
    assert [select_family_proof_scope(entries, hour_slot=h)[-1]
            for h in range(3)] == [5, 15, 60]
    assert [select_family_proof_scope(entries, hour_slot=h)[-1]
            for h in range(3, 6)] == [5, 15, 60]


@pytest.mark.parametrize("hour", [-1, -12])
def test_reject_invalid_hour_slot(hour):
    with pytest.raises(ValueError, match="slot"):
        select_family_proof_scope(_streams(), hour_slot=hour)
