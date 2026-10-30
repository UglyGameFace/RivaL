"""Source clocks, shared requests, contract identity and actionable failure reasons."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app import polymarket_watch as poly

TOKENS = ["1" * 60, "2" * 60]
CONDITION = "0x" + "a" * 64


@pytest.fixture
def source(monkeypatch):
    clock = [datetime(2026, 10, 30, 21, 30, 30, tzinfo=UTC)]
    requests = []
    controls = {"age_up": 0, "age_down": 0, "mutate_book": lambda book: None,
                "mutate_event": lambda event: None}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(poly, "datetime", Clock)

    async def handle(req):
        requests.append(req)
        if req.url.host == "gamma-api.polymarket.com":
            slug = req.url.path.rsplit("/", 1)[-1]
            start = datetime.fromtimestamp(int(slug.rsplit("-", 1)[-1]), UTC)
            event = {
                "slug": slug, "closed": False, "archived": False, "active": True,
                "eventStartTime": start.isoformat(),
                "endDate": (start + timedelta(minutes=5)).isoformat(),
                "markets": [{"closed": False, "active": True,
                             "enableOrderBook": True, "acceptingOrders": True,
                             "outcomes": '["Up","Down"]', "conditionId": CONDITION,
                             "clobTokenIds": json.dumps(TOKENS)}],
            }
            controls["mutate_event"](event)
            return httpx.Response(200, json=event)
        token = req.url.params["token_id"]
        age = controls["age_up" if token == TOKENS[0] else "age_down"]
        book = {
            "asset_id": token, "market": CONDITION,
            "timestamp": str(int((clock[0] - timedelta(seconds=age)).timestamp() * 1000)),
            "asks": [{"price": "0.52", "size": "30"}],
        }
        controls["mutate_book"](book)
        return httpx.Response(200, json=book)

    actual = httpx.AsyncClient
    controls["client"] = actual
    monkeypatch.setattr(poly.httpx, "AsyncClient", lambda *a, **kw: actual(
        transport=httpx.MockTransport(handle), timeout=kw["timeout"],
    ))
    return clock, requests, controls


@pytest.mark.asyncio
@pytest.mark.parametrize("display_first", [True, False])
async def test_board_and_alert_share_gamma_and_both_books(source, display_first):
    clock, requests, _ = source
    reader = poly.PolymarketReader()
    if display_first:
        board = await reader.window_board("BTC", 5)
        alert = await reader.market("BTC", 5)
    else:
        alert = await reader.market("BTC", 5)
        board = await reader.window_board("BTC", 5)
    assert board.current_quote is alert
    assert len([r for r in requests if r.url.host.startswith("gamma")]) == 2
    assert len([r for r in requests if r.url.host.startswith("clob")]) == 2
    clock[0] += timedelta(minutes=5)
    next_alert = await reader.market("BTC", 5)
    assert next_alert.slug != alert.slug


@pytest.mark.asyncio
async def test_concurrent_board_and_alert_share_exact_event(source):
    _, requests, _ = source
    reader = poly.PolymarketReader()
    board, alert = await asyncio.gather(reader.window_board("BTC", 5), reader.market("BTC", 5))
    assert board.current_quote is alert
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_source_clock_is_oldest_book_and_cache_never_renews_age(source):
    clock, _, controls = source
    controls.update(age_up=24, age_down=3)
    reader = poly.PolymarketReader()
    alert = await reader.market("BTC", 5)
    assert alert.received_at == clock[0]
    assert alert.quote_at == clock[0] - timedelta(seconds=24)
    # The cache still has time left, but the source snapshot has expired.
    controls["age_up"] = 26
    clock[0] += timedelta(seconds=2)
    assert await reader.market("BTC", 5) is None
    assert reader._reasons[("BTC", 5)][1] == "book_stale"


@pytest.mark.asyncio
async def test_display_and_alert_timing_are_independent_in_same_cache(source):
    clock, requests, _ = source
    clock[0] = clock[0].replace(minute=33, second=29)  # 91s until close
    reader = poly.PolymarketReader()
    alert = await reader.market("BTC", 5)
    assert alert is not None
    clock[0] += timedelta(seconds=2)
    assert await reader.market("BTC", 5) is None
    assert await reader.market("BTC", 5, display_only=True) is alert
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,reason", [
    ("asset_id", TOKENS[1], "book_identity"),
    ("market", "0x" + "b" * 64, "book_identity"),
    ("timestamp", "NaN", "book_timestamp_invalid"),
    ("timestamp", "1e300", "book_timestamp_invalid"),
    ("timestamp", None, "book_timestamp_missing"),
    ("asks", [], "no_liquidity"),
])
async def test_bad_books_fail_closed_with_fixed_diagnostic(source, field, value, reason, caplog):
    _, _, controls = source
    controls["mutate_book"] = lambda b: b.update({field: value})
    reader = poly.PolymarketReader()
    with caplog.at_level("INFO"):
        board = await reader.window_board("BTC", 5)
    assert board.current is not None and board.current_quote is None
    assert board.quote_unavailable_reason == reason
    assert f"reason={reason}" in caplog.text
    assert "reason=ValueError" not in caplog.text


@pytest.mark.asyncio
async def test_missing_order_acceptance_does_not_become_a_notice(source):
    _, _, controls = source
    controls["mutate_event"] = lambda e: e["markets"][0].pop("acceptingOrders")
    reader = poly.PolymarketReader()
    board = await reader.window_board("BTC", 5)
    assert board.current is not None and not board.current.accepting_orders
    assert board.current_quote is None
    assert board.quote_unavailable_reason == "orders_unavailable"


@pytest.mark.asyncio
async def test_cancelled_consumer_does_not_cancel_shared_gamma(monkeypatch, source):
    clock, _, controls = source
    reader = poly.PolymarketReader()
    started, release = asyncio.Event(), asyncio.Event()
    hits = []

    async def handle(req):
        hits.append(req)
        started.set()
        await release.wait()
        return httpx.Response(404)

    monkeypatch.setattr(poly.httpx, "AsyncClient", lambda *a, **kw: controls["client"](
        transport=httpx.MockTransport(handle), timeout=3.5,
    ))
    slug = poly.market_slug("BTC", 5, clock[0])
    one = asyncio.create_task(reader._event(slug, now=clock[0], refresh=False))
    await started.wait()
    two = asyncio.create_task(reader._event(slug, now=clock[0], refresh=False))
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    release.set()
    with pytest.raises(poly.MarketUnavailable, match="event_not_found"):
        await two
    assert len(hits) == 1
    assert not reader._event_pending


@pytest.mark.asyncio
async def test_future_listing_recomputed_at_window_open(source):
    clock, requests, _ = source
    clock[0] = clock[0].replace(minute=34, second=59)
    reader = poly.PolymarketReader()
    opens = clock[0] + timedelta(seconds=1)
    before = await reader.listed("BTC", 5, opens)
    assert not before.accepting_orders
    clock[0] = opens
    after = await reader.listed("BTC", 5, opens)
    assert after.accepting_orders
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_http_connection_pool_is_shared_and_closed(source):
    reader = poly.PolymarketReader()
    await reader.window_board("BTC", 5)
    client = reader._client
    assert client is not None and not client.is_closed
    await reader.market("ETH", 5)
    assert reader._client is client
    await reader.aclose()
    assert client.is_closed
    assert reader._client is None and not reader._event_pending


@pytest.mark.asyncio
async def test_total_market_deadline_cancels_slow_lookup(source, monkeypatch):
    reader = poly.PolymarketReader()
    cancelled = asyncio.Event()

    async def slow(**kw):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(reader, "_lookup", slow)
    monkeypatch.setattr(poly, "READ_TIMEOUT", 0.01)
    assert await reader.market("BTC", 5) is None
    assert cancelled.is_set()
    assert reader._reasons[("BTC", 5)][1] == "request_timeout"
