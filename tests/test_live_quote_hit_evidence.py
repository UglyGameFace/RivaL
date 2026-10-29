"""Public quotes and empirical hit-model gates refuse stale/fake evidence."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.price_targets import PriceGoal
from parlay_bot.perpetuals.live_quotes import (
    BookQuote,
    PublicBestQuoteClient,
    QuoteUnavailable,
    parse_book_quote,
    quote_cost_preview,
    quote_embed,
)
from parlay_bot.perpetuals.price_hit_evidence import evaluate_price_hit_model
from parlay_bot.storage.hot import SQLiteHotStore


def _payload(now, symbol="BTCUSDT"):
    return {
        "symbol": symbol, "bidPrice": "100.0", "bidQty": "0.5",
        "askPrice": "100.1", "askQty": "0.25",
        "time": int(now.timestamp() * 1000),
    }


def test_verified_best_bid_ask_is_not_candle_nor_guaranteed_fill():
    now = datetime.now(UTC)
    quote = parse_book_quote(_payload(now), asset="BTC", received_at=now)
    assert quote.bid == 100.0 and quote.ask == 100.1
    assert quote.exchange_at <= now
    preview = quote_cost_preview(quote, quantity=0.2)
    assert preview["top_book_sufficient"]
    assert preview["assumed_net_bps"] < 0
    assert not preview["actual_profit_verified"]
    assert "Assumption" not in quote_embed(quote, quantity=0.2).title
    assert not quote_cost_preview(quote, quantity=2)["top_book_sufficient"]
    with pytest.raises(ValueError):
        quote_cost_preview(quote, quantity=math.nan)
    with pytest.raises(ValueError):
        quote_cost_preview(quote, quantity=0)
    with pytest.raises(QuoteUnavailable):
        quote.validate(now=now + timedelta(minutes=1))


@pytest.mark.parametrize("change", [
    {"symbol": "ETHUSDT"}, {"bidPrice": "NaN"},
    {"bidPrice": "-1"}, {"bidPrice": "105"},
    {"askQty": "0"}, {"askPrice": "200"},
    {"time": 1000000}, {"time": "fake"},
])
def test_malformed_wrong_contract_crossed_old_or_no_liquidity_rejected(change):
    now = datetime.now(UTC)
    payload = _payload(now)
    payload.update(change)
    with pytest.raises(QuoteUnavailable):
        parse_book_quote(payload, asset="BTC", received_at=now)


@pytest.mark.asyncio
async def test_one_shared_public_book_cache_and_rate_control():
    hits = []

    def handler(request):
        hits.append(request)
        assert request.url.path == "/fapi/v1/ticker/bookTicker"
        symbol = request.url.params["symbol"]
        asset = symbol.removesuffix("USDT")
        return httpx.Response(200, json=_payload(datetime.now(UTC), f"{asset}USDT"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = PublicBestQuoteClient(client=client)
    try:
        a = await source.get("BTC")
        b = await source.get("BTC")
        c = await source.get("ETH")
        assert a == b and c.asset == "ETH"
        assert len(hits) == 2
        with pytest.raises(QuoteUnavailable):
            await source.get("DOGE")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_geoblock_fails_closed_with_cooldown_no_repeated_requests():
    hits = []

    def blocked(request):
        hits.append(request)
        return httpx.Response(451, json={"error": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    source = PublicBestQuoteClient(client=client)
    try:
        with pytest.raises(QuoteUnavailable, match="region"):
            await source.get("BTC")
        with pytest.raises(QuoteUnavailable, match="temporarily"):
            await source.get("BTC")
        assert len(hits) == 1
    finally:
        await client.aclose()


def test_proof_is_not_qualified_without_real_forward_outcomes(tmp_path):
    store = SQLiteHotStore(tmp_path / "no-fake-probability.sqlite")
    store.initialize()
    goal = PriceGoal(
        "105", datetime.now(UTC), "gate", "BTC_USDT", "USDT", 100,
    )
    try:
        absent = evaluate_price_hit_model(
            None, goal=goal, horizon_minutes=5,
        )
        assert absent.probability is None and not absent.qualified
        from parlay_bot.perpetuals.market_store import PerpetualMarketStore

        market = PerpetualMarketStore(store)
        market.initialize()
        insufficient = evaluate_price_hit_model(
            market, goal=goal, horizon_minutes=15,
        )
        assert insufficient.probability is None
        assert "Insufficient" in insufficient.reason
        with pytest.raises(ValueError):
            evaluate_price_hit_model(market, goal=goal, horizon_minutes=30)
    finally:
        store.close()


def test_price_barrier_model_requires_source_matched_validated_chronology():
    from parlay_bot.perpetuals.price_hit_evidence import (
        _Sample,
        _insufficient,
        _volatility,
    )

    assert _volatility([1, 2, 4, 8]) >= 0
    assert not _insufficient(
        "gate", 5, "No independently heldout observations",
    ).qualified
    assert _Sample("2026-10-29", True, 1).hit == 1
