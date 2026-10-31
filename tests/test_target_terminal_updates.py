"""Cycle must deliver expiry, hit, and lean-flip edits, then stop terminal targets.

Expiry and lean-flip edits are covered with real market rows (not mocked
snapshots); a HIT or EXPIRED price target is stopped after its outcome
edit lands, so dead watches cannot linger and consume follow slots.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.price_targets import PriceGoal, price_result
from parlay_bot.discord_app.targeting import (
    DirectionTarget,
    DirectionTargetService,
    DirectionTargetStore,
)
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


class DummyMessage:
    def __init__(self, identifier: int) -> None:
        self.id = identifier
        self.edits = []
        self.embed = None

    async def edit(self, *, embed) -> None:
        self.edits.append(embed.to_dict())
        self.embed = embed


class DummyDM:
    def __init__(self) -> None:
        self.id = 12345678
        self.messages = {}
        self.sent = 0

    async def send(self, *, embed):
        self.sent += 1
        msg = DummyMessage(self.sent + 100)
        msg.embed = embed
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, msg_id):
        return self.messages[msg_id]


class DummyBot:
    def __init__(self, dm) -> None:
        self.dm = dm

    def get_channel(self, channel_id):
        return self.dm if channel_id == self.dm.id else None

    async def fetch_channel(self, channel_id):
        assert channel_id == self.dm.id
        return self.dm


def _market(tmp_path, now):
    store = SQLiteHotStore(tmp_path / "probe.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    closed = now - timedelta(minutes=5)
    opened = closed - timedelta(minutes=5) + timedelta(milliseconds=1)
    candle = MarketCandle(
        "gate", "BTC_USDT", "USDT", 5, opened, closed,
        closed + timedelta(milliseconds=1),
        104, 108, 101, 106, 10.0, 34,
    )
    market.ingest(MarketBatch((), (candle,), 5, closed + timedelta(milliseconds=1)))
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=closed,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    ForwardAccuracyMonitor(market).initialize()
    return store, market


@pytest.mark.asyncio
async def test_price_goal_expiry_edit_is_delivered(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store, market = _market(tmp_path, now)
    dm = DummyDM()
    bot = DummyBot(dm)
    service = DirectionTargetService(
        bot=bot, market=market, store=DirectionTargetStore(store))
    # Goal started 6 minutes ago (deadline passed) with an UP target the
    # price never touched: reference 106, target 200.
    goal = PriceGoal(
        price="200", started_at=now - timedelta(minutes=6),
        venue="gate", contract="BTC_USDT", quote="USDT", reference_price=106.0,
    )
    # Stored fingerprint is the WATCH state from before expiry.
    watch = price_result(
        market, asset="BTC", horizon_minutes=5, goal=goal,
        now=now - timedelta(minutes=2),
    )
    assert watch.status == "WATCH"
    msg = await dm.send(embed=__import__("discord").Embed(title="seed"))
    service.store.save(DirectionTarget(
        "987654321", str(dm.id), str(msg.id), "BTC", 5,
        watch.fingerprint, goal,
    ))
    changed = await service.cycle()
    assert changed == 1
    assert msg.embed.title == "BTC • 5m • ⌛ EXPIRED"
    # Terminal outcome delivered: the row is stopped (the DM keeps its
    # final embed) instead of lingering as a dead watch.
    assert service.store.get("987654321") is None
    # No further edits on later cycles.
    assert await service.cycle() == 0


@pytest.mark.asyncio
async def test_price_goal_hit_edit_is_delivered_and_stops(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "hit.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    # Reference candle closes before the watch window opens.
    closed1 = now - timedelta(minutes=10)
    opened1 = closed1 - timedelta(minutes=5) + timedelta(milliseconds=1)
    market.ingest(MarketBatch(
        (), (MarketCandle(
            "gate", "BTC_USDT", "USDT", 5, opened1, closed1,
            closed1 + timedelta(milliseconds=1),
            99, 101, 98, 100, 10.0, 34,
        ),), 5, closed1 + timedelta(milliseconds=1),
    ))
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=closed1,
        new_candles=1, total_candles=1, last_candle_close=closed1,
    )
    ForwardAccuracyMonitor(market).initialize()
    dm = DummyDM()
    bot = DummyBot(dm)
    service = DirectionTargetService(
        bot=bot, market=market, store=DirectionTargetStore(store))
    goal = PriceGoal(
        price="105", started_at=now - timedelta(minutes=2),
        venue="gate", contract="BTC_USDT", quote="USDT", reference_price=100.0,
    )
    watch = price_result(
        market, asset="BTC", horizon_minutes=5, goal=goal,
        now=now - timedelta(minutes=1),
    )
    assert watch.status == "WATCH"
    msg = await dm.send(embed=__import__("discord").Embed(title="seed"))
    service.store.save(DirectionTarget(
        "987654321", str(dm.id), str(msg.id), "BTC", 5,
        watch.fingerprint, goal,
    ))
    # A window candle closes above the UP target after the watch starts.
    closed2 = now - timedelta(minutes=1)
    opened2 = closed2 - timedelta(minutes=5) + timedelta(milliseconds=1)
    market.ingest(MarketBatch(
        (), (MarketCandle(
            "gate", "BTC_USDT", "USDT", 5, opened2, closed2,
            closed2 + timedelta(milliseconds=1),
            104, 108, 101, 106, 10.0, 34,
        ),), 5, closed2 + timedelta(milliseconds=1),
    ))
    assert await service.cycle() == 1
    assert msg.embed.title == "BTC • 5m • ✅ HIT"
    assert service.store.get("987654321") is None
    assert await service.cycle() == 0


@pytest.mark.asyncio
async def test_direction_lean_flip_edit_is_delivered(tmp_path, monkeypatch):
    # This tests single-message update mechanics. Prospective qualification
    # is tested independently and is required in the real tracker.
    monkeypatch.setattr(
        ForwardAccuracyMonitor, 'venue_summary',
        lambda *_a, **_kw: {'model_beats_baseline': True, 'watch_evidence_passes': True},
    )
    now = datetime.now(UTC).replace(microsecond=0)
    store, market = _market(tmp_path, now)
    dm = DummyDM()
    bot = DummyBot(dm)
    service = DirectionTargetService(
        bot=bot, market=market, store=DirectionTargetStore(store))
    opened = (now - timedelta(minutes=10)) - timedelta(minutes=5) + timedelta(
        milliseconds=1)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("gate", "BTC_USDT", "USDT", "BTC", 5, MODEL,
             opened.isoformat(), 106.0,
             (now - timedelta(seconds=45)).isoformat(),
             (now + timedelta(minutes=2)).isoformat(),
             0.68, 0.5, 120),
        )
    from parlay_bot.discord_app.targeting import target_snapshot

    up = target_snapshot(market, asset="BTC", horizon_minutes=5)
    assert up.direction == "UP"
    msg = await dm.send(embed=__import__("discord").Embed(title="seed"))
    service.store.save(DirectionTarget(
        "987654321", str(dm.id), str(msg.id), "BTC", 5, up.fingerprint, None,
    ))
    assert await service.cycle() == 0
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("gate", "BTC_USDT", "USDT", "BTC", 5, MODEL,
             (opened + timedelta(minutes=5)).isoformat(), 106.0,
             (now - timedelta(seconds=10)).isoformat(),
             (now + timedelta(minutes=2)).isoformat(),
             0.30, 0.5, 120),
        )
    assert await service.cycle() == 1
    assert "DOWN" in msg.embed.title
    # Direction watches never terminate on their own: the row stays for
    # the next flip.
    assert service.store.get("987654321") is not None

