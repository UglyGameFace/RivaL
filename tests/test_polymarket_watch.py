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
    service.polymarket.active = AsyncMock(return_value=[_notice(now)])
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
