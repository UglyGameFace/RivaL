"""Slow research and outbox preflight must not turn stale books into alerts."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app import auto_hunt
from parlay_bot.discord_app.polymarket_watch import PolymarketNotice
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.fixture
def delivery(tmp_path):
    hot = SQLiteHotStore(tmp_path / "delivery.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "polymarket", True)
    notice = PolymarketNotice(
        asset="BTC", horizon=5, issued=now - timedelta(seconds=44),
        expires=now + timedelta(minutes=4), slug="test-latency-window",
        up_ask=.55, down_ask=.47, quote_at=now - timedelta(seconds=5),
        received_at=now,
    )
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=555)))
    user = SimpleNamespace(dm_channel=channel, create_dm=AsyncMock())
    service = auto_hunt.AutoHuntService(
        bot=SimpleNamespace(get_user=lambda _: user, fetch_user=AsyncMock()),
        market=object(), quotes=object(), store=store,
    )
    service.polymarket.active = AsyncMock(return_value=[notice])
    yield service, store, channel, notice
    hot.close()


@pytest.mark.asyncio
async def test_polymarket_delivers_while_forecast_scan_is_still_waiting(delivery, monkeypatch):
    service, store, channel, _ = delivery
    store.set_tier("1001", "qualified", True)
    started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow(*a, **kw):
        started.set()
        await release.wait()
        return []

    async def send(**kwargs):
        sent.set()
        return SimpleNamespace(id=555)

    channel.send.side_effect = send
    monkeypatch.setattr(auto_hunt, "qualified_watches", slow)
    monkeypatch.setattr(auto_hunt, "new_forecast_watches", AsyncMock(return_value=[]))
    cycle = asyncio.create_task(service.cycle())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(sent.wait(), timeout=1)
        assert not cycle.done()
    finally:
        release.set()
    assert await cycle == 1
    assert store.delivery_health("1001") == {"sent": 1}


@pytest.mark.asyncio
async def test_next_polymarket_cycle_does_not_wait_for_forecast_cycle(delivery, monkeypatch):
    service, store, _, _ = delivery
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(*a, **kw):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(auto_hunt, "new_forecast_watches", slow)
    cycle = asyncio.create_task(service.cycle(scope="forecasts"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await asyncio.wait_for(service.cycle(scope="polymarket"), timeout=1) == 1
        # The ongoing forecast cycle does not own the next public market scan.
        assert await asyncio.wait_for(service.cycle(scope="polymarket"), timeout=1) == 0
        assert service.polymarket.active.await_count == 2
        assert not cycle.done()
    finally:
        release.set()
    assert await cycle == 0
    assert store.delivery_health("1001") == {"sent": 1}


@pytest.mark.asyncio
async def test_failed_research_producer_does_not_cancel_market_notices(delivery, monkeypatch):
    service, _, channel, _ = delivery
    monkeypatch.setattr(auto_hunt, "new_forecast_watches", AsyncMock(side_effect=RuntimeError()))
    assert await service.cycle() == 1
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_polymarket_metrics_separate_window_age_from_local_delay(delivery, monkeypatch, caplog):
    service, store, _, notice = delivery

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return notice.received_at

    monkeypatch.setattr(auto_hunt, "datetime", Clock)
    assert store.claim("1001", notice, now=notice.received_at)
    with caplog.at_level("INFO"):
        result = await service._send_dm("1001", notice, notice.received_at, tier=None)
    assert result is not None
    assert "window_age_ms=44000" in caplog.text
    assert "observed_to_attempt_ms=0" in caplog.text
    assert "source_quote_age_ms=5000" in caplog.text
    assert "status=sent" in caplog.text
    assert "issue_to_attempt_ms" not in caplog.text


@pytest.mark.asyncio
async def test_quote_expiring_during_pg_checkout_is_never_sent(delivery, monkeypatch):
    service, store, channel, notice = delivery
    clock = [notice.received_at]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(auto_hunt, "datetime", Clock)
    assert store.claim("1001", notice, now=clock[0])
    begin = store.begin_delivery

    def slow_checkout(*a, **kw):
        result = begin(*a, **kw)
        clock[0] += timedelta(seconds=21)  # old 5s source book is now stale
        return result

    monkeypatch.setattr(store, "begin_delivery", slow_checkout)
    result = await service._send_dm("1001", notice, clock[0], tier=None)
    assert result is None
    channel.send.assert_not_awaited()
    store.finish("1001", notice, sent=False)
    assert not store.claim("1001", notice, now=clock[0] + timedelta(seconds=30))


@pytest.mark.asyncio
async def test_stop_during_producer_read_prevents_notice(delivery):
    service, store, channel, notice = delivery
    started, release = asyncio.Event(), asyncio.Event()

    async def read():
        started.set()
        await release.wait()
        return [notice]

    service.polymarket.active = read
    cycle = asyncio.create_task(service.cycle(scope="polymarket"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await service.stop(1001)
    release.set()
    assert await cycle == 0
    channel.send.assert_not_awaited()
    assert store.delivery_health("1001") == {}


@pytest.mark.asyncio
async def test_stale_cached_notice_is_rejected_before_claim(delivery):
    service, store, channel, notice = delivery
    stale = replace(notice, quote_at=datetime.now(UTC) - timedelta(seconds=26))
    service.polymarket.active = AsyncMock(return_value=[stale])
    assert await service.cycle(scope="polymarket") == 0
    assert store.delivery_health("1001") == {}
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_only_polymarket_during_scan_prevents_notice(delivery):
    service, store, channel, notice = delivery
    started, release = asyncio.Event(), asyncio.Event()

    async def read():
        started.set()
        await release.wait()
        return [notice]

    service.polymarket.active = read
    cycle = asyncio.create_task(service.cycle(scope="polymarket"))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not (await service.toggle_tier(1001, "polymarket"))[0]
    release.set()
    assert await cycle == 0
    assert store.enabled("1001")
    channel.send.assert_not_awaited()
