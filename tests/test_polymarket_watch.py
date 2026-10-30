"""Live Polymarket source must only alert correctly identified, current events."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from parlay_bot.discord_app import auto_hunt, polymarket_watch
from parlay_bot.storage.hot import SQLiteHotStore


def run(coroutine):
    return asyncio.run(coroutine)


def _notice(now):
    start = datetime.fromtimestamp(
        int(now.timestamp()) // 300 * 300, tz=UTC,
    )
    return polymarket_watch.PolymarketNotice(
        asset="BTC", horizon=5, issued=start,
        expires=start + timedelta(minutes=5),
        slug=polymarket_watch.market_slug("BTC", 5, now),
        up_ask=0.56, down_ask=0.48, quote_at=now,
    )


def test_polymarket_source_select_is_user_specific_persistent_and_stoppable(tmp_path):
    store = SQLiteHotStore(tmp_path / "poly-select.sqlite")
    store.initialize()
    hunt = auto_hunt.AutoHuntStore(store)
    hunt.initialize()
    now = datetime.now(UTC)
    try:
        assert hunt.source("101") == "forecasts"
        assert hunt.subscribe("101", now=now)
        hunt.choose_source("101", "polymarket")
        assert hunt.source("101") == "polymarket"
        again = auto_hunt.AutoHuntStore(store)
        assert again.subscribers()[0]["source"] == "polymarket"
        assert not again.claim(
            "102", _notice(now), now=now,
        )
        assert again.claim("101", _notice(now), now=now)
        assert not again.claim("101", _notice(now), now=now)
        assert again.stop("101")
        assert again.subscribers() == []
        assert again.source("101") == "polymarket"
    finally:
        store.close()


def test_polymarket_live_market_only_from_real_gamma_and_clob(monkeypatch):
    now = datetime.fromtimestamp(1793349000 + 60, UTC)
    # Fixed clock keeps the real quote-age and expiry tests deterministic.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(polymarket_watch, "datetime", Clock)
    slug = polymarket_watch.market_slug("BTC", 5, now)
    start = datetime.fromtimestamp(1793349000, UTC)
    expire = start + timedelta(minutes=5)
    tokens = ["1" * 60, "2" * 60]
    response = {
        "slug": slug, "active": True, "closed": False, "archived": False,
        "eventStartTime": start.isoformat(), "endDate": expire.isoformat(),
        "markets": [{
            "active": True, "closed": False, "enableOrderBook": True,
            "acceptingOrders": True, "outcomes": '["Up","Down"]',
            "clobTokenIds": json.dumps(tokens),
        }],
    }
    requested = []
    def handler(req: httpx.Request) -> httpx.Response:
        requested.append(str(req.url))
        if req.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=response)
        if req.url.host == "clob.polymarket.com":
            token_id = req.url.params["token_id"]
            price = 0.56 if token_id == tokens[0] else 0.48
            return httpx.Response(200, json={
                "timestamp": str(int(now.timestamp() * 1000)),
                "asks": [{"price": str(price), "size": "20"}],
            })
        return httpx.Response(400)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        polymarket_watch.httpx, "AsyncClient",
        lambda *a, **k: real_client(
            transport=httpx.MockTransport(handler), timeout=k["timeout"],
        ),
    )
    reader = polymarket_watch.PolymarketReader()
    result = run(reader.market("BTC", 5, now=now))
    assert result is not None
    assert (result.up_ask, result.down_ask) == (0.56, 0.48)
    assert result.url == f"https://polymarket.com/event/{slug}"
    assert len(requested) == 3
    assert run(reader.market("BTC", 5, now=now)) == result
    assert len(requested) == 3  # 15-second data cache

    response["markets"][0]["outcomes"] = '["Down","Up"]'
    assert run(reader.market(
        "BTC", 5, now=now, opened=start,
    )) is None
    response["markets"][0]["outcomes"] = '["Up","Down"]'
    response["eventStartTime"] = (start - timedelta(minutes=5)).isoformat()
    assert run(reader.market(
        "BTC", 5, now=now, opened=start,
    )) is None


def test_market_rejects_stale_and_no_liquidity():
    now = datetime.now(UTC)
    import pytest
    with pytest.raises(ValueError, match="stale"):
        polymarket_watch._best_ask(
            {"timestamp": str(int((now - timedelta(minutes=3)).timestamp() * 1000)),
             "asks": [{"price": "0.42", "size": "1"}]},
            now=now,
        )
    with pytest.raises(ValueError, match="liquidity"):
        polymarket_watch._best_ask(
            {"timestamp": str(int(now.timestamp() * 1000)),
             "asks": [{"price": "0.01", "size": "0"}]},
            now=now,
        )


def test_polymarket_alert_works_without_rival_collector(tmp_path):
    now = datetime.now(UTC)
    hot = SQLiteHotStore(tmp_path / "poly-only.sqlite")
    hot.initialize()
    hunt = auto_hunt.AutoHuntStore(hot)
    hunt.initialize()
    hunt.subscribe("101", now=now)
    hunt.choose_source("101", "polymarket")
    channel = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id=555)),
    )
    user = SimpleNamespace(dm_channel=channel)
    bot = SimpleNamespace(get_user=lambda _: user, fetch_user=AsyncMock())
    service = auto_hunt.AutoHuntService(
        bot=bot, market=None, quotes=None, store=hunt,
    )
    # Exercise delivery with a valid mocked window irrespective of the
    # wall-clock position near a 5-minute boundary. Real market validation
    # and expiry rejection are covered independently.
    from dataclasses import replace

    current = replace(_notice(now), expires=now + timedelta(minutes=4))
    service.polymarket.active = AsyncMock(return_value=[current])
    try:
        assert run(service.cycle()) == 1
        assert run(service.cycle()) == 0
        assert channel.send.await_count == 1
        embed = channel.send.await_args.kwargs["embed"]
        assert "Polymarket" in embed.title
        assert "NOT calibrated" in embed.description
        assert hunt.stop("101")
        assert run(service.cycle()) == 0
    finally:
        hot.close()


def test_verified_current_and_next_polymarket_targets_without_fabrication(monkeypatch):
    now = datetime.fromtimestamp(1793349000 + 271, UTC)
    start = datetime.fromtimestamp(1793349000, UTC)
    next_start = start + timedelta(minutes=5)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(polymarket_watch, "datetime", Clock)
    requested = []
    tokens = ["1" * 60, "2" * 60]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        requested.append(url)
        if req.url.host == "gamma-api.polymarket.com":
            slug = url.rsplit("/", 1)[-1]
            if not slug.endswith((
                str(int(start.timestamp())),
                str(int(next_start.timestamp())),
            )):
                return httpx.Response(404)
            opened = (
                next_start if slug.endswith(str(int(next_start.timestamp())))
                else start
            )
            return httpx.Response(200, json={
                "slug": slug, "active": True, "closed": False, "archived": False,
                "eventStartTime": opened.isoformat(),
                "endDate": (opened + timedelta(minutes=5)).isoformat(),
                "eventMetadata": (
                    {"priceToBeat": "75050.25", "other": "ignored"}
                    if opened == start else {"priceToBeat": "999999"}
                ),
                "markets": [{
                    "active": True, "closed": False,
                    "acceptingOrders": True, "enableOrderBook": True,
                    "outcomes": json.dumps(["Up", "Down"]),
                    "clobTokenIds": json.dumps(tokens),
                }],
            })
        if req.url.host == "clob.polymarket.com":
            token = req.url.params["token_id"]
            return httpx.Response(200, json={
                "timestamp": str(int(now.timestamp() * 1000)),
                "asks": [{"price": "0.61" if token == tokens[0] else "0.42",
                          "size": "20"}],
            })
        return httpx.Response(400)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        polymarket_watch.httpx, "AsyncClient",
        lambda *a, **kw: real_client(
            transport=httpx.MockTransport(handler), timeout=kw["timeout"],
        ),
    )
    reader = polymarket_watch.PolymarketReader()
    board = run(reader.window_board("XRP", 5, now=now))
    assert board.current is not None
    assert board.next_window is not None
    assert board.current.price_to_beat == 75050.25
    assert board.current.accepting_orders
    assert board.current_quote is not None
    assert board.current_quote.up_ask == 0.61
    assert board.current_quote.down_ask == 0.42
    assert board.next_window.price_to_beat is None
    assert board.next_window.accepting_orders is False
    assert board.next_window.url.endswith(
        f"/xrp-updown-5m-{int(next_start.timestamp())}"
    )
    assert len([x for x in requested if "/book?" in x]) == 2
    assert len([x for x in requested if "/events/slug/" in x]) == 3
    # The informational late-window quote must not bypass the original
    # >= 90-second alerting rule or populate its notification cache.
    assert run(reader.market("XRP", 5, now=now)) is None


def test_next_polymarket_listing_never_invented_when_gamma_disagrees(monkeypatch):
    now = datetime.fromtimestamp(1793349000 + 20, UTC)
    start = datetime.fromtimestamp(1793349000, UTC)

    def handler(req: httpx.Request) -> httpx.Response:
        slug = str(req.url).rsplit("/", 1)[-1]
        opened = start + timedelta(minutes=5)
        return httpx.Response(200, json={
            "slug": slug, "closed": False, "archived": False,
            "eventStartTime": (opened + timedelta(seconds=15)).isoformat(),
            "endDate": (opened + timedelta(minutes=5)).isoformat(),
            "markets": [{
                "closed": False,
                "outcomes": '["Up","Down"]',
            }],
        })

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        polymarket_watch.httpx, "AsyncClient",
        lambda *a, **kw: real_client(
            transport=httpx.MockTransport(handler), timeout=kw["timeout"],
        ),
    )
    reader = polymarket_watch.PolymarketReader()
    result = run(reader.listed(
        "BTC", 5, start + timedelta(minutes=5), now=now,
    ))
    assert result is None
    assert run(reader.listed(
        "BTC", 5, start + timedelta(minutes=5), now=now,
    )) is None
