"""Only exact, immutable Kalshi contract evidence may reach beginner menus."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.kalshi_public import (
    KalshiCryptoBrowser,
    KalshiMarket,
    parse_markets,
)
from parlay_bot.perpetuals.kalshi_forward import KalshiForwardResearch
from parlay_bot.storage.hot import SQLiteHotStore


def _market(n: int, at: datetime) -> KalshiMarket:
    return KalshiMarket(
        ticker=f"KXBTC15M-{n}", title="Bitcoin exact outcome",
        close_time=at + timedelta(minutes=15),
        yes_ask=0.48, no_ask=0.55,
        rules="Official Kalshi source rule",
        series_ticker="KXBTC15M",
        quote_source="orderbook", yes_ask_size=10.0, no_ask_size=10.0,
    )


def _settlement(market: KalshiMarket, at: datetime) -> dict:
    return {"market": {
        "ticker": market.ticker, "status": "settled",
        "result": "yes" if int(market.ticker.rsplit("-", 1)[-1]) % 2 else "no",
        "settlement_ts": (at + timedelta(minutes=20)).isoformat(),
    }}


def test_exact_market_preview_remains_research_only_and_immutable(tmp_path):
    store = SQLiteHotStore(tmp_path / "kalshi-preview.sqlite")
    forward = KalshiForwardResearch(store)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    first = _market(0, start)
    assert forward.observe(asset="BTC", market=first, at=start)
    untrained = forward.contract_preview(asset="BTC", market=first)
    assert untrained["candidate_yes"] is None
    assert untrained["approved_model"] is False
    assert forward.contract_preview(
        asset="BTC", market=replace(first, rules="Changed settlement rule")
    ) is None
    assert forward.contract_preview(
        asset="BTC", market=replace(first, close_time=first.close_time + timedelta(hours=1))
    ) is None
    assert forward.contract_preview(asset="ETH", market=first) is None

    for n in range(100):
        at = start + timedelta(minutes=30 * n)
        market = _market(n, at)
        forward.observe(asset="BTC", market=market, at=at)
        assert forward.settle(
            ticker=market.ticker, payload=_settlement(market, at),
            checked_at=at + timedelta(minutes=21),
        )
    at = start + timedelta(minutes=3000)
    next_market = _market(100, at)
    assert forward.observe(asset="BTC", market=next_market, at=at)
    preview = forward.contract_preview(asset="BTC", market=next_market)
    assert preview["ticker"] == next_market.ticker
    assert preview["series_ticker"] == "KXBTC15M"
    assert preview["candidate_training_labels"] == 100
    assert 0 < preview["candidate_yes"] < 1
    assert preview["approved_model"] is False
    assert preview["verified_after_cost"] is False

    # More labels must not rewrite the candidate already frozen at issuance.
    other = _market(101, at + timedelta(minutes=30))
    assert forward.observe(asset="BTC", market=other, at=at + timedelta(minutes=30))
    assert forward.settle(
        ticker=other.ticker,
        payload=_settlement(other, at + timedelta(minutes=30)),
        checked_at=at + timedelta(minutes=51),
    )
    assert forward.contract_preview(asset="BTC", market=next_market) == preview
    store.close()


@pytest.mark.asyncio
async def test_browser_reads_own_kalshi_candidate_without_network(tmp_path):
    store = SQLiteHotStore(tmp_path / "browser-preview.sqlite")
    forward = KalshiForwardResearch(store)
    browser = KalshiCryptoBrowser(forward=forward)
    at = datetime(2026, 8, 1, tzinfo=UTC)
    market = _market(1, at)
    try:
        preview = await browser.contract_research(
            asset="BTC", market=market, observed_at=at,
        )
        assert preview["ticker"] == market.ticker
        assert preview["candidate_yes"] is None
        # Not a Polymarket fallback, not a newly invented live prediction.
        assert preview["approved_model"] is False
        assert await browser.contract_research(
            asset="ETH", market=market, observed_at=at,
        ) is None
    finally:
        await browser.aclose()
        store.close()

def test_long_settlement_rules_are_not_truncated_before_hashing():
    now = datetime(2026, 10, 30, tzinfo=UTC)
    original = "Kalshi settlement clause " + "x" * 350 + " YES"
    changed = "Kalshi settlement clause " + "x" * 350 + " NO"

    def fetch(rules):
        return parse_markets({"markets": [{
            "ticker": "KXBTC15M-FUTURE", "title": "Bitcoin",
            "status": "open",
            "close_time": (now + timedelta(minutes=15)).isoformat(),
            "yes_ask_dollars": "0.40",
            "no_ask_dollars": "0.61",
            "rules_primary": rules,
        }]}, now=now, series_ticker="KXBTC15M")[0]

    assert fetch(original).rules == original
    assert fetch(changed).rules == changed
    assert fetch(original).rules != fetch(changed).rules
