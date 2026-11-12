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
    with pytest.raises((ValueError, TypeError)):
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
            assert market.book_observed_at is not None
            assert len(market.book_receipt_sha256) == 64
            assert result.checked_at >= market.book_observed_at
            if browser._study_tasks:
                await __import__("asyncio").gather(*browser._study_tasks)
            preview = forward.contract_preview(asset="BTC", market=market)
            assert preview is not None
            assert preview["baseline_yes"] == pytest.approx(0.54 / 1.16)
            assert not preview["approved_model"]
            proof = forward.executable_quote_evidence(
                ticker=market.ticker, quantity=10)
            assert proof["status"] == "fee_schedule_unverified"
            assert proof["book_transport_sha256"] == market.book_receipt_sha256
            assert proof["book_received_at"] == market.book_observed_at.isoformat()
            assert proof["yes_side_depth_sufficient"]
            assert proof["no_side_depth_sufficient"]
            assert not proof["verified_after_cost"]
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


@pytest.mark.asyncio
async def test_automatic_series_event_fee_receipts_bind_exact_first_book(tmp_path):
    """Public fee metadata + both change endpoints precede issuance."""
    store = SQLiteHotStore(tmp_path / "fee-auto.sqlite")
    forward = KalshiForwardResearch(store)
    called = []

    def respond(req: httpx.Request) -> httpx.Response:
        called.append((req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/series/fee_changes"):
            assert req.url.params["series_ticker"] == "KXBTC15M"
            return httpx.Response(
                200, json={"series_fee_change_arr": [], "cursor": ""})
        if req.url.path.endswith("/events/fee_changes"):
            assert req.url.params["event_ticker"] == "KXBTC15M-EVENT"
            return httpx.Response(200, json={"event_fee_changes": [], "cursor": ""})
        if req.url.path.endswith("/events/KXBTC15M-EVENT"):
            return httpx.Response(200, json={"event": {
                "event_ticker": "KXBTC15M-EVENT",
                "series_ticker": "KXBTC15M",
                "fee_type_override": "quadratic_with_maker_fees",
                "fee_multiplier_override": 0.5,
            }})
        if req.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [{
                "ticker": "KXBTC15M", "title": "Bitcoin",
                "category": "Crypto", "fee_type": "quadratic",
                "fee_multiplier": 1.0,
            }]})
        if req.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{
                "ticker": "KXBTC15M-EVENT-ABCDE",
                "event_ticker": "KXBTC15M-EVENT",
                "title": "BTC quote and fees", "status": "open",
                "close_time": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "rules_primary": "Official Kalshi contract-specific rule",
                "yes_ask_dollars": "0.02", "no_ask_dollars": "0.02",
            }]})
        if req.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=_book())
        raise AssertionError(req.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        browser = KalshiCryptoBrowser(client=client, forward=forward)
        try:
            result = await browser.browse("BTC")
            assert len(result.markets) == 1
            m = result.markets[0]
            assert m.fee_evidence["status"] == "fee_metadata_verified"
            assert m.fee_evidence["fee_type"] == "quadratic_with_maker_fees"
            assert m.fee_evidence["fee_multiplier"] == .5
            assert m.fee_evidence["source"]["level"] == "event_override"
            if browser._study_tasks:
                await __import__("asyncio").gather(*browser._study_tasks)
            proof = forward.executable_quote_evidence(
                ticker=m.ticker, quantity=10)
            assert proof["status"] == "fee_metadata_verified_paper_estimate"
            assert proof["fee_schedule_verified"] is True
            assert proof["fee_type"] == "quadratic_with_maker_fees"
            assert proof["fee_multiplier"] == .5
            assert proof["yes_paper_fee"]["estimated_model_fee_usd"] > 0
            assert proof["no_paper_fee"]["estimated_model_fee_usd"] > 0
            assert not proof["verified_after_cost"]
            assert not proof["executed_trade"]
            assert any(path.endswith("/events/fee_changes") for path, _ in called)
            assert any(path.endswith("/series/fee_changes") for path, _ in called)
        finally:
            await browser.aclose()
            store.close()


@pytest.mark.asyncio
async def test_missing_exact_event_fee_never_falls_back_to_series_as_proven(tmp_path):
    """Real series fee alone is not exact-event economic evidence."""
    store = SQLiteHotStore(tmp_path / "missing-event.sqlite")
    forward = KalshiForwardResearch(store)

    def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [{
                "ticker": "KXBTC15M", "title": "Bitcoin",
                "category": "Crypto", "fee_type": "quadratic",
                "fee_multiplier": 1.0,
            }]})
        if req.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{
                "ticker": "KXBTC15M-EVENT-FAIL",
                "event_ticker": "KXBTC15M-EVENT",
                "title": "BTC fee-source outage", "status": "open",
                "close_time": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "rules_primary": "Source specific",
            }]})
        if req.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=_book())
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        browser = KalshiCryptoBrowser(client=client, forward=forward)
        try:
            market = (await browser.browse("BTC")).markets[0]
            assert market.fee_evidence is None
            if browser._study_tasks:
                await __import__("asyncio").gather(*browser._study_tasks)
            receipt = forward.executable_quote_evidence(ticker=market.ticker)
            assert receipt["status"] == "fee_schedule_unverified"
            assert not receipt["fee_schedule_verified"]
        finally:
            await browser.aclose()
            store.close()
