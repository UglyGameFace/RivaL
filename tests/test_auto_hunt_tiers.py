"""Auto-Hunt tier reconciliation: EARLY_RESEARCH vs QUALIFIED_WATCH,
independent toggles, additive migration, and delivery guarantees.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app import auto_hunt
from parlay_bot.storage.hot import SQLiteHotStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tier_store(tmp_path):
    hot = SQLiteHotStore(tmp_path / "tiers.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    return store


def _candidate(now, **replace):
    args = {
        "asset": "BTC", "horizon": 5, "venue": "hyperliquid",
        "issued": now - timedelta(seconds=30),
        "expires": now + timedelta(minutes=4),
        "probability_up": 0.68, "bid": 81000.0, "ask": 81001.0,
        "quote_at": now, "engine": "live",
        "model_version": "prospective-logistic-shadow-v2",
    }
    args.update(replace)
    return auto_hunt.HuntCandidate(**args)


class _Channel:
    def __init__(self):
        self.sends = []
        self.id = 999

    async def send(self, *args, **kwargs):
        self.sends.append(kwargs.get("embed"))
        message = SimpleNamespace(id=1000 + len(self.sends))
        return message


class _User:
    def __init__(self, channel):
        self.dm_channel = channel
        self.id = 1001

    async def create_dm(self):
        return self.dm_channel


class _Bot:
    def __init__(self, user):
        self._user = user

    def get_user(self, user_id):
        return self._user

    async def fetch_user(self, user_id):
        return self._user


def _service(bot, store, *, market=None, quotes=None):
    return auto_hunt.AutoHuntService(
        bot=bot, market=market, quotes=quotes, store=store,
    )


# ---------------------------------------------------------------- migration

def test_migration_backfills_legacy_sources_without_losing_rows(tier_store):
    store = tier_store
    # Legacy state: two subscribers with single-source rows and delivery rows.
    store.subscribe("1001", now=datetime.now(UTC) - timedelta(days=1))
    store.choose_source("1001", "forecasts")
    store.subscribe("1002", now=datetime.now(UTC) - timedelta(days=1))
    store.choose_source("1002", "polymarket")
    old = _candidate(datetime.now(UTC))
    store.claim("1001", old, now=datetime.now(UTC))
    store.finish("1001", old, sent=True, message_id="555")
    # Re-running initialize (as a restart does) must backfill exactly once
    # and leave every pre-existing row untouched.
    store.initialize()
    store.initialize()
    assert store.tiers("1001") == {
        "research": True, "qualified": False, "polymarket": False}
    assert store.tiers("1002") == {
        "research": False, "qualified": False, "polymarket": True}
    assert store.enabled("1001") is True
    assert store.source("1001") == "forecasts"
    assert store.source("1002") == "polymarket"
    with store.store.connect() as conn:
        row = conn.execute(
            "SELECT state, message_id FROM rival_hunt_delivery "
            "WHERE user_id = '1001'"
        ).fetchone()
    assert row["state"] == "sent" and row["message_id"] == "555"
    # A subscriber with no source row keeps the old default: forecasts.
    store.subscribe("1003", now=datetime.now(UTC))
    store.initialize()
    assert store.tiers("1003")["research"] is True


def test_toggles_are_independent_and_stop_clears_all(tier_store):
    store = tier_store
    store.subscribe("1001", now=datetime.now(UTC))
    # A bare subscription keeps the legacy default: research alerts.
    assert store.tiers("1001")["research"] is True
    # A first explicit toggle seeds from the effective flags, so research
    # is not silently dropped when qualified is switched on.
    store.set_tier("1001", "qualified", True)
    assert store.tiers("1001") == {
        "research": True, "qualified": True, "polymarket": False}
    store.set_tier("1001", "polymarket", True)
    assert store.tiers("1001") == {
        "research": True, "qualified": True, "polymarket": True}
    store.set_tier("1001", "research", False)
    assert store.tiers("1001")["qualified"] is True
    assert store.tiers("1001")["polymarket"] is True
    assert store.stop("1001") is True
    assert store.tiers("1001") == {
        "research": False, "qualified": False, "polymarket": False}
    assert store.enabled("1001") is False
    with pytest.raises(ValueError):
        store.set_tier("1001", "bogus", True)


def test_service_toggle_subscribes_on_first_enable(tier_store):
    channel = _Channel()
    service = _service(_Bot(_User(channel)), tier_store)
    state, _ = run(service.toggle_tier(1001, "qualified"))
    assert state is True
    assert tier_store.enabled("1001") is True
    assert tier_store.tiers("1001")["qualified"] is True
    state, _ = run(service.toggle_tier(1001, "qualified"))
    assert state is False
    assert tier_store.enabled("1001") is True  # toggling off keeps history
    assert tier_store.tiers("1001")["qualified"] is False


def test_subscribe_sets_only_its_own_flag(tier_store):
    channel = _Channel()
    user = _User(channel)
    service = _service(_Bot(user), tier_store, market=object())
    ok, _ = run(service.subscribe(user, source="forecasts"))
    assert ok is True
    assert tier_store.tiers("1001") == {
        "research": True, "qualified": False, "polymarket": False}
    ok, _ = run(service.subscribe(user, source="polymarket"))
    assert ok is True
    assert tier_store.tiers("1001") == {
        "research": True, "qualified": False, "polymarket": True}
    assert tier_store.source("1001") == "polymarket"  # legacy row kept


# ------------------------------------------------------------------- embeds

def test_qualified_embed_shows_quote_and_edge_only_when_positive():
    now = datetime.now(UTC)
    base = _candidate(now, screened=True)
    embed = auto_hunt.hunt_embed(base, observed_at=now, tier="qualified")
    assert "QUALIFIED WATCH" in embed.title
    assert "Bid **$81,000.00**" in embed.description
    assert "not a bet opportunity" in embed.description
    assert "Bet opportunity terms" not in embed.description
    rich = _candidate(now, screened=True, after_cost_bps=12.5,
                      after_cost_status="PROVEN")
    embed = auto_hunt.hunt_embed(rich, observed_at=now, tier="qualified")
    assert "Bet opportunity terms" not in embed.description
    assert "Historical simulated after-cost return: **+12.5 bps**" in embed.description
    assert "Current betting edge: **NOT ESTABLISHED**" in embed.description
    flat = _candidate(now, screened=True, after_cost_bps=-3.0)
    embed = auto_hunt.hunt_embed(flat, observed_at=now, tier="qualified")
    assert "Bet opportunity terms" not in embed.description
    assert "not a bet opportunity" in embed.description


def test_research_embed_never_frames_a_wager():
    now = datetime.now(UTC)
    embed = auto_hunt.hunt_embed(_candidate(now), observed_at=now)
    assert "EARLY RESEARCH" in embed.description
    assert "Never a wager" in embed.description
    assert "NOT VERIFIED" in embed.description
    assert "not calibrated odds" in embed.description
    assert "Bet opportunity terms" not in embed.description


# ------------------------------------------------------- qualified producer

def _signal_stub(decisions):
    """decisions: {(asset, horizon): decision dict or None}."""
    calls = []

    async def stub(market, quotes, *, asset, horizon_minutes, now,
                   stream_shadow, decision_out, decision_only=False,
                   deadline=None):
        assert decision_only is True
        calls.append((asset, horizon_minutes))
        decision = decisions.get((asset, horizon_minutes))
        if decision is not None:
            decision_out.update(decision)
        import discord as _discord
        return _discord.Embed(title=f"{asset} | {horizon_minutes}m • stub")

    stub.calls = calls
    return stub


def _watch_decision(now, *, is_watch=True, quote_age=0, after_bps=None):
    return {
        "asset": "BTC", "horizon_minutes": 5, "venue": "hyperliquid",
        "engine": "live", "model_version": "prospective-logistic-shadow-v2",
        "issued": now - timedelta(seconds=30),
        "target": now + timedelta(minutes=4),
        "direction": "UP", "probability": 0.68, "is_watch": is_watch,
        "bid": 81000.0, "ask": 81001.0,
        "quote_at": now - timedelta(seconds=quote_age),
        "after_cost_bps": after_bps, "after_cost_status": None,
    }


@pytest.mark.asyncio
async def test_qualified_producer_requires_watch_and_fresh_quote(monkeypatch):
    now = datetime.now(UTC)
    stub = _signal_stub({("BTC", 5): _watch_decision(now)})
    monkeypatch.setattr(auto_hunt, "signal_embed", stub)
    picks = await auto_hunt.qualified_watches(
        object(), object(), stream_shadow=None, now=now)
    assert len(picks) == 1
    assert picks[0].screened is True
    assert picks[0].bid == 81000.0
    # Not a watch: nothing.
    stub = _signal_stub({("BTC", 5): _watch_decision(now, is_watch=False)})
    monkeypatch.setattr(auto_hunt, "signal_embed", stub)
    assert await auto_hunt.qualified_watches(
        object(), object(), stream_shadow=None, now=now) == []
    # Stale quote: the signal would no longer validate it.
    stub = _signal_stub({("BTC", 5): _watch_decision(now, quote_age=30)})
    monkeypatch.setattr(auto_hunt, "signal_embed", stub)
    assert await auto_hunt.qualified_watches(
        object(), object(), stream_shadow=None, now=now) == []
    # One slot failing never blocks the others.
    assert len(stub.calls) == 6


@pytest.mark.asyncio
async def test_qualified_producer_ignores_errors_per_slot(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("book down")
    monkeypatch.setattr(auto_hunt, "signal_embed", boom)
    assert await auto_hunt.qualified_watches(
        object(), object(), stream_shadow=None,
        now=datetime.now(UTC)) == []


# ------------------------------------------------------------- cycle: tiers


@pytest.mark.asyncio
async def test_qualified_beats_research_for_the_same_window(monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "prio.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "research", True)
    store.set_tier("1001", "qualified", True)
    qualified = _candidate(now, screened=True)
    research = _candidate(now, venue="binance")
    assert qualified.window == research.window
    monkeypatch.setattr(auto_hunt, "qualified_watches",
                        AsyncMock(return_value=[qualified]))
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[research]))
    monkeypatch.setattr(auto_hunt.PolymarketReader, "active",
                        AsyncMock(return_value=[]))
    assert await service.cycle() == 1
    assert len(channel.sends) == 1
    assert "QUALIFIED WATCH" in channel.sends[0].title
    # A second cycle sends nothing: both tiers' claims already exist.
    assert await service.cycle() == 0
    assert len(channel.sends) == 1


@pytest.mark.asyncio
async def test_qualified_windows_are_not_hidden_by_cooldown(monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "cool.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "qualified", True)
    first = _candidate(now, screened=True)
    second = _candidate(now, screened=True,
                        issued=now - timedelta(seconds=10),
                        expires=now + timedelta(minutes=9))
    assert first.window != second.window
    monkeypatch.setattr(auto_hunt, "qualified_watches",
                        AsyncMock(return_value=[first]))
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[]))
    assert await service.cycle() == 1
    # Seconds later, a distinct qualified window still alerts: no cooldown.
    monkeypatch.setattr(auto_hunt, "qualified_watches",
                        AsyncMock(return_value=[second]))
    assert await service.cycle() == 1
    assert len(channel.sends) == 2


@pytest.mark.asyncio
async def test_research_keeps_cooldown_and_daily_cap(monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "cap.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    now = datetime.now(UTC)
    # Cooldown user: one send, then a distinct fresh window is still gated.
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "research", True)
    first = _candidate(now)
    second = _candidate(now, issued=now - timedelta(seconds=5),
                        expires=now + timedelta(minutes=9))
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[first]))
    assert await service.cycle() == 1
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[second]))
    assert await service.cycle() == 0
    assert len(channel.sends) == 1
    # Cap user: no prior sends (no cooldown), but a full budget blocks.
    store.subscribe("1002", now=now - timedelta(minutes=5))
    store.set_tier("1002", "research", True)
    for i in range(auto_hunt.DAILY_CAP_PER_TIER):
        with store.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_hunt_delivery
                   (user_id, forecast_key, asset, horizon, venue, issued_at,
                    expires_at, attempted_at, state)
                   VALUES (?, ?, 'ETH', 15, 'hyperliquid', ?, ?, ?, 'sent')""",
                ("1002", f"research-{i}",
                 (now - timedelta(hours=i + 1)).isoformat(),
                 (now - timedelta(hours=i)).isoformat(), now.isoformat()),
            )
    assert store.count_sent_today("1002", "research", now) == 12
    assert await service.cycle() == 0
    # The qualified budget is untouched by the research cap.
    assert store.count_sent_today("1002", "qualified", now) == 0


@pytest.mark.asyncio
async def test_delivery_race_claims_once_and_restart_does_not_resend(
        monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "race.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    race_watch = _candidate(now)
    results = await asyncio.gather(*[
        asyncio.to_thread(store.claim, "1001", race_watch, now=now)
        for _ in range(8)
    ])
    assert results.count(True) == 1
    # Two concurrent cycles over a fresh window: the claim serializes
    # delivery to one DM. The cycle user is separate from the race user so
    # the race claim's cooldown timestamp cannot gate the cycles.
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    store.subscribe("1002", now=now - timedelta(minutes=5))
    store.set_tier("1002", "research", True)
    cycle_watch = _candidate(now, issued=now - timedelta(seconds=5))
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[cycle_watch]))
    first, second = await asyncio.gather(service.cycle(), service.cycle())
    assert first + second == 1
    assert len(channel.sends) == 1
    # A brand-new service instance (a restart) still does not resend.
    service2 = _service(_Bot(_User(channel)), store)
    assert await service2.cycle() == 0
    assert len(channel.sends) == 1


@pytest.mark.asyncio
async def test_source_switching_preserves_other_tiers_and_history(
        monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "switch.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "qualified", True)
    store.set_tier("1001", "research", True)
    # Enabling Polymarket adds a flag; it must not clear the others.
    store.set_tier("1001", "polymarket", True)
    assert store.tiers("1001") == {
        "research": True, "qualified": True, "polymarket": True}
    store.set_tier("1001", "research", False)
    assert store.tiers("1001")["qualified"] is True
    assert store.tiers("1001")["polymarket"] is True
    # Legacy source writes still land, and old delivery rows survive.
    old = _candidate(now)
    store.claim("1001", old, now=now)
    store.finish("1001", old, sent=True, message_id="777")
    store.choose_source("1001", "polymarket")
    assert store.source("1001") == "polymarket"
    with store.store.connect() as conn:
        row = conn.execute(
            "SELECT state FROM rival_hunt_delivery WHERE user_id='1001'"
        ).fetchone()
    assert row["state"] == "sent"


@pytest.mark.asyncio
async def test_polymarket_is_disabled_by_default_and_flag_gated(
        monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "poly.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "research", True)
    assert store.tiers("1001")["polymarket"] is False
    from parlay_bot.discord_app.polymarket_watch import PolymarketNotice
    notice = PolymarketNotice(
        asset="BTC", horizon=5, issued=now,
        expires=now + timedelta(minutes=4), slug="btc-updown-5m-1",
        up_ask=0.55, down_ask=0.45, quote_at=now,
    )
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(auto_hunt.PolymarketReader, "active",
                        AsyncMock(return_value=[notice]))
    assert await service.cycle() == 0
    store.set_tier("1001", "polymarket", True)
    assert await service.cycle() == 1
    assert len(channel.sends) == 1
    assert "Polymarket" in channel.sends[0].title
    assert "not a RivaL prediction" in channel.sends[0].description


@pytest.mark.asyncio
async def test_expired_targets_never_alert_in_any_tier(monkeypatch, tmp_path):
    hot = SQLiteHotStore(tmp_path / "exp.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    now = datetime.now(UTC)
    store.subscribe("1001", now=now - timedelta(minutes=5))
    store.set_tier("1001", "research", True)
    store.set_tier("1001", "qualified", True)
    stale = _candidate(now, expires=now + timedelta(seconds=30))
    monkeypatch.setattr(auto_hunt, "qualified_watches",
                        AsyncMock(return_value=[stale]))
    monkeypatch.setattr(auto_hunt, "new_forecast_watches",
                        AsyncMock(return_value=[stale]))
    assert await service.cycle() == 0
    assert channel.sends == []


def test_status_reports_all_three_flags(tier_store):
    store = tier_store
    channel = _Channel()
    service = _service(_Bot(_User(channel)), store)
    assert run(service.status(1001)) == "OFF • choose a source to start"
    store.subscribe("1001", now=datetime.now(UTC))
    store.set_tier("1001", "qualified", True)
    status = run(service.status(1001))
    assert status.startswith("ON •")
    # Seeding preserves the legacy research default when qualified is added.
    assert "Research on" in status
    assert "Qualified on" in status
    assert "Polymarket off" in status
    store.set_tier("1001", "research", False)
    assert "Research off" in run(service.status(1001))
