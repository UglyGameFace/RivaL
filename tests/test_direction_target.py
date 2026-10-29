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
        assert targets.can_follow("123") is True
        targets.save(initial)
        assert targets.can_follow("123") is True
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
        assert "Target stopped" in first.embed.title
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


def test_fifteen_minute_target_does_not_expire_after_only_five(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store, market = market_with_prediction(tmp_path, now)
    try:
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "gate", "BTC_USDT", "USDT", "BTC", 15, MODEL,
                    (now - timedelta(minutes=14)).isoformat(), 106.0,
                    (now - timedelta(minutes=12)).isoformat(),
                    (now - timedelta(minutes=8)).isoformat(),
                    0.24, 0.5, 120,
                ),
            )
        live = target_snapshot(
            market, asset="BTC", horizon_minutes=15, now=now,
        )
        # The target candle opened 8m ago and closed 3m ago: this call
        # should already be WAIT, not falsely valid for 15 more minutes.
        assert live.direction == "WAIT"
        with store.connect() as conn:
            conn.execute(
                """UPDATE perp_forward_audit
                   SET target_opened_at=?, issued_at=?
                   WHERE asset='BTC' AND horizon_minutes=15""",
                (
                    (now + timedelta(minutes=2)).isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                ),
            )
        live = target_snapshot(market, asset="BTC", horizon_minutes=15, now=now)
        assert live.direction == "DOWN"
        assert live.ends_at == now + timedelta(minutes=7)
        assert target_snapshot(
            market, asset="BTC", horizon_minutes=15,
            now=now + timedelta(minutes=8),
        ).direction == "WAIT"
    finally:
        store.close()


def test_target_button_is_present_in_real_opportunities_view(tmp_path):
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.bot import RivalDiscordClient
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard

    client = RivalDiscordClient(Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "controls.sqlite"),
        discord_token=None,
    ))
    try:
        view = ResearchDashboard(
            owner_id=123, market=None, ai=None, venues=(),
            target_service=client.direction_targets,
        )
        controls = {item.custom_id for item in view.children}
        assert "rival:research:follow-target" in controls
        assert client.tree.get_command("rival-tools").get_command("target") is not None
        assert client.tree.get_command("rival-stop") is not None
        view.screen = "accuracy"
        view._rebuild()
        assert "rival:research:follow-target" not in {
            item.custom_id for item in view.children
        }
    finally:
        client.store.close()


@pytest.mark.asyncio
async def test_collector_outage_changes_existing_up_card_to_wait(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "offline.sqlite")
    dm = DummyDM()
    service = DirectionTargetService(
        bot=DummyBot(dm), market=object(), store=DirectionTargetStore(store),
    )
    try:
        monkeypatch.setattr(
            "parlay_bot.discord_app.targeting.target_snapshot",
            lambda _market, *, asset, horizon_minutes: TargetSnapshot(
                asset, horizon_minutes, "UP", "Current model.", "gate", 0.64, 103.0,
            ) if _market is not None else TargetSnapshot(
                asset, horizon_minutes, "WAIT", "Collector unavailable.",
            ),
        )
        assert (await service.follow(DummyUser(dm), asset="BTC", horizon_minutes=5))[0]
        card = dm.messages[101]
        assert "UP" in card.embed.title
        service.market = None
        assert await service.cycle() == 1
        assert "WAIT" in card.embed.title
        assert await service.cycle() == 0
    finally:
        store.close()


def test_automatic_dm_does_not_show_weak_51_percent_as_a_green_buy_signal():
    now = datetime.now(UTC)
    weak = TargetSnapshot(
        "BTC", 5, "UP", "Very weak model observation. Not an approved trade.",
        venue="hyperliquid", probability=.514, price=81001,
        issued_at=now, ends_at=now + timedelta(minutes=4),
    )
    message = target_embed(weak)
    assert "NO SIGNAL" in message.title
    assert "UP LEAN" in message.title
    assert message.color == message.color.orange()
    assert "very weak" in next(
        f.value for f in message.fields if f.name == "Uncalibrated model strength"
    )
    strong = TargetSnapshot(
        "BTC", 5, "DOWN", "Directional research. Not an approved trade.",
        venue="hyperliquid", probability=.62, price=81001,
        issued_at=now, ends_at=now + timedelta(minutes=4),
    )
    confident = target_embed(strong)
    assert "DOWN WATCH" in confident.title
    assert "Not an approved trade" in confident.description
