"""Kalshi public bids, not unfillable market summaries, ground model evidence."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.kalshi_public import (
    KalshiCryptoBrowser,
    KalshiMarket,
    parse_orderbook,
)
from parlay_bot.perpetuals.kalshi_forward import KalshiForwardResearch
from parlay_bot.storage.hot import SQLiteHotStore


def _book(yes=None, no=None):
    return {"orderbook_fp": {
        "yes_dollars": yes if yes is not None else [["0.3800", "11.0"]],
        "no_dollars": no if no is not None else [["0.4600", "17.0"]],
    }}


def test_orderbook_asks_use_opposite_best_bid_with_positive_depth():
    result = parse_orderbook(_book(
        yes=[["0.2000", "5.00"], ["0.3800", "11.00"], ["0.2400", "100.00"]],
        no=[["0.2500", "2.00"], ["0.4600", "17.00"]],
    ))
    assert result == (0.54, 0.62, 17.0, 11.0)
    assert parse_orderbook(_book(yes=[], no=[])) == (None, None, None, None)
    assert parse_orderbook(_book(yes=[], no=[["0.2000", "3.00"]])) == (
        0.8, None, 3.0, None,
    )


@pytest.mark.parametrize("bad", [
    {},
    {"orderbook_fp": {"yes_dollars": []}},
    _book(yes=[["nan", "10.00"]]),
    _book(yes=[["0.50", "-1.0"]]),
    _book(no=[["0.50", "0.00"]]),
    _book(yes=[[0.2, 3.0]]),
    _book(no=[["1.50", "1.00"]]),
    _book(yes=[["0.50"]]),
])
def test_malformed_or_unfillable_orderbook_cannot_become_a_prediction(bad):
    with pytest.raises(ValueError):
        parse_orderbook(bad)


@pytest.mark.asyncio
async def test_public_best_bid_overrides_misleading_market_summary_and_is_learned(
    tmp_path,
):
    store = SQLiteHotStore(tmp_path / "book.sqlite")
    forward = KalshiForwardResearch(store)
    calls = []
    def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [{
                "ticker": "KXBTC15M", "title": "Bitcoin", "category": "Crypto",
            }]})
        if req.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{
                "ticker": "KXBTC15M-REAL", "title": "BTC above threshold",
                "status": "open",
                "close_time": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "rules_primary": "Exact independent BTC oracle",
                # These summary numbers intentionally do not match the book.
                "yes_ask_dollars": "0.02", "no_ask_dollars": "0.02",
            }]})
        if req.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=_book())
        raise AssertionError("Unexpected Kalshi request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        browser = KalshiCryptoBrowser(client=client, forward=forward)
        try:
            result = await browser.browse("BTC")
            assert len(calls) == 3
            market = result.markets[0]
            assert market.quote_source == "orderbook"
            assert (market.yes_ask, market.no_ask) == (0.54, 0.62)
            assert (market.yes_ask_size, market.no_ask_size) == (17, 11)
            if browser._study_tasks:
                await __import__("asyncio").gather(*browser._study_tasks)
            preview = forward.contract_preview(asset="BTC", market=market)
            assert preview is not None
            assert preview["baseline_yes"] == pytest.approx(0.54 / 1.16)
            assert not preview["approved_model"]
            with store.connect() as db:
                row = db.execute(
                    "SELECT yes_ask,no_ask FROM rival_kalshi_forward WHERE ticker=?",
                    (market.ticker,),
                ).fetchone()
            assert (row["yes_ask"], row["no_ask"]) == (0.54, 0.62)
        finally:
            await browser.aclose()
            store.close()


@pytest.mark.asyncio
async def test_book_outage_fails_closed_without_recording_summary_prices(tmp_path):
    store = SQLiteHotStore(tmp_path / "failed-book.sqlite")
    forward = KalshiForwardResearch(store)
    def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [{
                "ticker": "KXBTC15M", "title": "Bitcoin", "category": "Crypto",
            }]})
        if req.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{
                "ticker": "KXBTC15M-REAL", "title": "BTC above threshold",
                "status": "open",
                "close_time": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "rules_primary": "Official rule",
                "yes_ask_dollars": "0.30", "no_ask_dollars": "0.71",
            }]})
        return httpx.Response(503, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        browser = KalshiCryptoBrowser(client=client, forward=forward)
        try:
            result = await browser.browse("BTC")
            market = result.markets[0]
            assert market.quote_source == "unavailable"
            assert market.yes_ask is None and market.no_ask is None
            assert not forward.observe(asset="BTC", market=market, at=result.checked_at)
            assert forward.contract_preview(asset="BTC", market=market) is None
            if browser._study_tasks:
                await __import__("asyncio").gather(*browser._study_tasks)
            assert forward.summary(asset="BTC")["observed"] == 0
        finally:
            await browser.aclose()
            store.close()


def test_legacy_market_summary_cannot_create_new_kalshi_evidence(tmp_path):
    store = SQLiteHotStore(tmp_path / "legacy.sqlite")
    forward = KalshiForwardResearch(store)
    at = datetime(2026, 10, 1, tzinfo=UTC)
    indicative = KalshiMarket(
        ticker="KXBTC15M-123", title="Bitcoin", close_time=at + timedelta(minutes=15),
        yes_ask=0.35, no_ask=0.67, rules="Actual Kalshi settlement",
        series_ticker="KXBTC15M", quote_source="market_summary",
    )
    assert not forward.observe(asset="BTC", market=indicative, at=at)
    assert forward.contract_preview(asset="BTC", market=indicative) is None
    one_sided = replace(
        indicative, quote_source="orderbook", yes_ask_size=3.0,
        no_ask_size=None,
    )
    assert not forward.observe(asset="BTC", market=one_sided, at=at)
    assert forward.summary(asset="BTC")["observed"] == 0
    store.close()
