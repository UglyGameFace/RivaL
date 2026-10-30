"""Real-time scanner, opt-in and Polymarket filter regression tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.auto_hunt import (
    AutoHuntService,
    AutoHuntStore,
    HuntCandidate,
    candidate_allowed,
    confirmed_direction_change,
)
from parlay_bot.discord_app.polymarket_watch import PolymarketNotice
from parlay_bot.perpetuals.live_direction import (
    LiveDirectionScanner,
    LiveShiftNotice,
)
from parlay_bot.storage.hot import SQLiteHotStore

CLOCK = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)


class DualVenue:
    def __init__(self):
        self.prices = {"hyperliquid": 100., "binance": 100.}
        self.stale = set()

    def snapshot(self, venue, asset, *, now):
        if venue in self.stale or asset != "BTC":
            return None
        mid = self.prices[venue]
        return SimpleNamespace(
            bid=mid - .005, ask=mid + .005,
            exchange_at=now,
        )


def test_five_second_websocket_scanning_requires_strong_confirmed_reversal():
    hub = DualVenue()
    scanner = LiveDirectionScanner(hub)
    events = []
    for step in range(31):
        now = CLOCK + timedelta(seconds=step * 5)
        price = 100 if step < 6 else 100.3 if step < 20 else 99.65
        hub.prices = {"hyperliquid": price, "binance": price * 1.00003}
        events.extend(scanner.observe(now=now))
    assert len(events) == 1
    assert events[0].asset == "BTC"
    assert events[0].direction == "DOWN"
    assert events[0].move_bps < -15
    assert events[0].venue == "live_trend"
    assert events[0].issued >= CLOCK + timedelta(seconds=110)
    assert scanner.observe(now=CLOCK+timedelta(seconds=150)) == []


def test_stale_source_or_disagreeing_venues_cannot_trigger_reversal():
    hub = DualVenue()
    scanner = LiveDirectionScanner(hub)
    for step in range(15):
        now = CLOCK+timedelta(seconds=step*5)
        hub.prices["hyperliquid"] = 100.5 if step > 5 else 100
        hub.prices["binance"] = 100.1 if step > 5 else 100
        assert scanner.observe(now=now) == []
    assert scanner._confirmed == {}
    hub.stale.add("binance")
    for step in range(15, 28):
        now = CLOCK+timedelta(seconds=step*5)
        assert scanner.observe(now=now) == []
    assert scanner._confirmed == {}


def test_model_reversal_uses_distinct_causal_forecast_issuance():
    now = CLOCK
    def candidate(*, issued, expires, p):
        return HuntCandidate(
            asset="BTC", horizon=5, venue="hyperliquid",
            issued=issued, expires=expires, probability_up=p,
            bid=None, ask=None, quote_at=None, engine="live", model_version="v1",
        )
    previous = candidate(
        issued=now-timedelta(minutes=6),
        expires=now+timedelta(minutes=1), p=.71,
    )
    latest = candidate(
        issued=now-timedelta(seconds=15),
        expires=now+timedelta(minutes=6), p=.25,
    )
    assert confirmed_direction_change(latest, previous, now=now)
    assert not confirmed_direction_change(latest, latest, now=now)
    assert not confirmed_direction_change(
        candidate(issued=latest.issued, expires=latest.expires, p=.48),
        previous, now=now,
    )


def test_personal_polymarket_filters_and_stop_keep_legacy_preferences(tmp_path):
    hot = SQLiteHotStore(tmp_path / "prefs.sqlite")
    hot.initialize()
    store = AutoHuntStore(hot)
    store.initialize()
    try:
        owner = "444"
        store.subscribe(owner, now=CLOCK-timedelta(hours=1))
        defaults = store.preferences(owner)
        assert defaults["assets"] == ("BTC", "ETH", "SOL")
        assert defaults["horizons"] == (5, 15)
        assert not defaults["live_shifts"]
        store.set_preference(owner, "assets", ("ETH",))
        store.set_preference(owner, "horizons", (15,))
        store.set_preference(owner, "side", "up")
        store.set_preference(owner, "max_ask_pct", 50)
        store.set_preference(owner, "min_remaining_seconds", 180)
        notice = PolymarketNotice(
            asset="ETH", horizon=15, issued=CLOCK,
            expires=CLOCK+timedelta(minutes=9), slug="eth-updown-15m-123456",
            up_ask=.45, down_ask=.7, quote_at=CLOCK,
        )
        assert candidate_allowed(notice, store.preferences(owner), now=CLOCK)
        assert not candidate_allowed(
            PolymarketNotice(
                asset="BTC", horizon=15, issued=CLOCK,
                expires=CLOCK+timedelta(minutes=9), slug="a",
                up_ask=.1, down_ask=.8, quote_at=CLOCK,
            ), store.preferences(owner), now=CLOCK,
        )
        assert not candidate_allowed(
            notice, store.preferences(owner),
            now=CLOCK+timedelta(minutes=8),
        )
        with pytest.raises(ValueError):
            store.set_preference(owner, "max_ask_pct;DROP TABLE x", 25)
        with pytest.raises(ValueError):
            store.set_preference(owner, "assets", ("DOGE",))
        store.set_preference(owner, "live_shifts", 1)
        store.set_preference(owner, "direction_changes", 1)
        restarted = AutoHuntStore(hot)
        restarted.initialize()
        assert restarted.preferences(owner)["live_shifts"]
        assert restarted.stop(owner)
        assert not restarted.preferences(owner)["live_shifts"]
        assert not restarted.preferences(owner)["direction_changes"]
        assert restarted.preferences(owner)["assets"] == ("ETH",)
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_background_shift_delivery_is_opt_in_and_deduped(tmp_path):
    hot = SQLiteHotStore(tmp_path / "live-alerts.sqlite")
    hot.initialize()
    store = AutoHuntStore(hot)
    store.initialize()
    now = datetime.now(UTC).replace(microsecond=0)
    notice = LiveShiftNotice(
        asset="BTC", issued=now, expires=now+timedelta(minutes=3),
        direction="DOWN", move_bps=-24., lookback_seconds=90,
    )
    service = AutoHuntService(
        bot=SimpleNamespace(), market=None, quotes=None, store=store,
    )
    service._send_dm = AsyncMock(return_value=SimpleNamespace(id=123))
    try:
        store.subscribe("900", now=now-timedelta(minutes=1))
        assert await service.live_cycle([notice]) == 0
        service._send_dm.assert_not_awaited()
        store.set_preference("900", "live_shifts", 1)
        assert await service.live_cycle([notice]) == 1
        assert await service.live_cycle([notice]) == 0
        assert service._send_dm.await_count == 1
        restarted = AutoHuntStore(hot)
        restarted.initialize()
        assert restarted.delivery_health("900")["sent"] == 1
        store.stop("900")
        assert await service.live_cycle([
            LiveShiftNotice(
                asset="BTC", issued=now+timedelta(seconds=1),
                expires=now+timedelta(minutes=3), direction="UP",
                move_bps=23., lookback_seconds=75,
            ),
        ]) == 0
    finally:
        hot.close()
