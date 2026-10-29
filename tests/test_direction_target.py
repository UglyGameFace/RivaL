"""Target users select market/timeframe; RivaL edits one private card only on changes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.targeting import (
    DirectionTarget,
    DirectionTargetService,
    DirectionTargetStore,
    TargetSnapshot,
    target_embed,
    target_snapshot,
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


class DummyUser:
    def __init__(self, dm) -> None:
        self.id = 987654321
        self.dm_channel = dm

    async def create_dm(self):
        return self.dm_channel


class DummyBot:
    def __init__(self, dm) -> None:
        self.dm = dm

    def get_channel(self, channel_id):
        return self.dm if channel_id == self.dm.id else None

    async def fetch_channel(self, channel_id):
        assert channel_id == self.dm.id
        return self.dm


def market_with_prediction(tmp_path, now):
    store = SQLiteHotStore(tmp_path / "target.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    closed = now - timedelta(minutes=1)
    opened = closed - timedelta(minutes=5) + timedelta(milliseconds=1)
    candle = MarketCandle(
        "gate", "BTC_USDT", "USDT", 5, opened, closed,
        closed + timedelta(milliseconds=1),
        104, 108, 101, 106, 10.0, 34,
    )
    market.ingest(MarketBatch(
        (), (candle,), 5, closed + timedelta(milliseconds=1),
    ))
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=closed,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    ForwardAccuracyMonitor(market).initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "gate", "BTC_USDT", "USDT", "BTC", 5, MODEL,
                opened.isoformat(), 106.0,
                (now - timedelta(seconds=45)).isoformat(),
                (now + timedelta(minutes=2)).isoformat(),
                0.68, 0.5, 120,
            ),
        )
    return store, market


def test_targets_are_simple_and_truthful_without_fabricated_forecasts(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store, market = market_with_prediction(tmp_path, now)
    try:
        ready = target_snapshot(market, asset="BTC", horizon_minutes=5, now=now)
        assert ready.direction == "UP"
        assert ready.price == 106
        assert ready.probability == pytest.approx(.68)
        assert "UP" in target_embed(ready).title
        assert "not an approved trade" in ready.detail.lower()
        assert target_snapshot(
            market, asset="BTC", horizon_minutes=15, now=now,
        ).direction == "WAIT"
        assert target_snapshot(
            market, asset="BTC", horizon_minutes=5,
            now=now + timedelta(hours=1),
        ).direction == "WAIT"
        assert target_snapshot(
            None, asset="ETH", horizon_minutes=15,
        ).direction == "WAIT"
        with pytest.raises(ValueError):
            target_snapshot(market, asset="DOGE", horizon_minutes=5)
        with pytest.raises(ValueError):
            target_snapshot(market, asset="BTC", horizon_minutes=30)
    finally:
        store.close()


def test_digest_stable_until_evidence_or_status_changes():
    base = TargetSnapshot("BTC", 5, "WAIT", "No feed.")
    assert base.fingerprint == TargetSnapshot("BTC", 5, "WAIT", "No feed.").fingerprint
    assert base.fingerprint != TargetSnapshot("BTC", 5, "DOWN", "New call.").fingerprint


def test_target_storage_replace_restart_and_cancellation(tmp_path):
    store = SQLiteHotStore(tmp_path / "persistent.sqlite")
    targets = DirectionTargetStore(store)
    initial = DirectionTarget("123", "444", "555", "BTC", 5, "digest1")
    try:
        targets.save(initial)
        targets.save(DirectionTarget("123", "444", "555", "SOL", 15, "digest2"))
        again = DirectionTargetStore(store)
        assert len(again.active()) == 1
        assert again.get("123").asset == "SOL"
        again.updated(initial, "bad-old-edit")
        assert again.get("123").fingerprint == "digest2"
        again.stop("123", message_id="different")
        assert again.get("123") is not None
        again.stop("123")
        assert again.active() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_private_follow_updates_same_dm_and_retargets_without_spam(
    tmp_path, monkeypatch,
):
    store = SQLiteHotStore(tmp_path / "follows.sqlite")
    dm = DummyDM()
    service = DirectionTargetService(
        bot=DummyBot(dm), market=None,
        store=DirectionTargetStore(store),
    )
    user = DummyUser(dm)
    try:
        ok, ack = await service.follow(user, asset="BTC", horizon_minutes=5)
        assert ok is True and "UP, DOWN, or WAIT" in ack
        assert dm.sent == 1
        first = dm.messages[101]
        assert "WAIT" in first.embed.title
        assert (await service.cycle()) == 0

        state = {"direction": "UP", "price": 99.0}

        def fake_snapshot(_market, *, asset, horizon_minutes):
            return TargetSnapshot(
                asset, horizon_minutes,
                state["direction"], "Prospective model watch.",
                "gate", .6, state["price"],
            )

        # Simulate a running source with a new independently issued forecast.
        service.market = object()
        monkeypatch.setattr(
            "parlay_bot.discord_app.targeting.target_snapshot", fake_snapshot,
        )
        assert (await service.cycle()) == 1
        assert len(first.edits) == 1
        assert "UP" in first.embed.title
        assert (await service.cycle()) == 0
        assert len(first.edits) == 1

        state["direction"] = "DOWN"
        state["price"] = 98.0
        assert (await service.cycle()) == 1
        assert "DOWN" in first.embed.title
        assert dm.sent == 1

        ok, _ = await service.follow(user, asset="ETH", horizon_minutes=15)
        assert ok
        assert dm.sent == 1
        assert "ETH" in first.embed.title
        assert service.store.get(str(user.id)).asset == "ETH"

        # The persisted subscription remains editable by a restarted worker.
        replacement = DirectionTargetService(
            bot=DummyBot(dm), market=object(), store=DirectionTargetStore(store),
        )
        state["direction"] = "WAIT"
        assert (await replacement.cycle()) == 1
        assert "WAIT" in first.embed.title
        assert (await replacement.stop(user.id)) is True
        assert (await replacement.cycle()) == 0
        assert replacement.store.active() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_targeting_never_creates_automatic_dm_without_opt_in(tmp_path):
    store = SQLiteHotStore(tmp_path / "no-opt-in.sqlite")
    dm = DummyDM()
    service = DirectionTargetService(
        bot=DummyBot(dm), market=object(),
        store=DirectionTargetStore(store),
    )
    try:
        assert await service.cycle() == 0
        assert dm.sent == 0
        assert await service.stop(6789) is False
    finally:
        store.close()
