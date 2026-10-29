"""Real exchange-candle target accounting; not made-up price-touch probabilities."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.price_targets import (
    PriceGoal,
    create_price_goal,
    parse_price,
    price_embed,
    price_result,
)
from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.targeting import (
    DirectionTarget,
    DirectionTargetService,
    DirectionTargetStore,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore
from tests.test_direction_target import DummyBot, DummyDM, DummyUser


def _seed(tmp_path, now):
    store = SQLiteHotStore(tmp_path / "goals.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    opened = now - timedelta(minutes=6)
    closed = now - timedelta(minutes=1)
    observed = closed + timedelta(milliseconds=1)
    _bar(market, opened=opened, closed=closed, observed=observed,
         low=99.0, high=101.0, close=100.0)
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=observed,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    return store, market


def _bar(market, *, opened, closed, observed, low, high, close):
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles
               (venue,contract,quote,interval_minutes,opened_at,closed_at,
                first_observed_at,open_price,high_price,low_price,close_price,
                volume_base,trades)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "gate", "BTC_USDT", "USDT", 5, opened.isoformat(),
                closed.isoformat(), observed.isoformat(),
                close, high, low, close, 1.0, 10,
            ),
        )


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"), 0, -1,
    "hello", "100.123456789", "1000000000001",
])
def test_reject_bad_user_target_prices(bad):
    with pytest.raises(ValueError):
        parse_price(bad)


def test_price_targets_use_explicit_goal_not_btc_quantity(tmp_path):
    now = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
    store, market = _seed(tmp_path, now)
    try:
        goal = create_price_goal(
            market, asset="BTC", horizon_minutes=15, target_price="105.00",
            now=now,
        )
        assert goal.price == "105"
        assert goal.venue == "gate"
        assert goal.reference_price == 100
        outcome = price_result(
            market, asset="BTC", horizon_minutes=15, goal=goal, now=now,
        )
        assert outcome.status == "WATCH"
        assert outcome.required_direction == "UP"
        assert outcome.hit_at is None
        assert "105.00" in price_embed(outcome).fields[0].value
        assert "probability is not validated" in outcome.explanation
        assert "directional" not in price_embed(outcome).footer.text.lower()
        assert price_result(
            market, asset="BTC", horizon_minutes=15, goal=goal,
            now=now + timedelta(minutes=16),
        ).status == "EXPIRED"
    finally:
        store.close()


def test_pre_opt_in_candle_high_cannot_fake_target_hit(tmp_path):
    now = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
    store, market = _seed(tmp_path, now)
    try:
        goal = create_price_goal(
            market, asset="BTC", horizon_minutes=5, target_price=105,
            now=now,
        )
        # The candle starts before subscription; its high could be BEFORE
        # the user opted in. Its close is below 105, so no verified HIT.
        _bar(
            market, opened=now - timedelta(seconds=20),
            closed=now + timedelta(minutes=4),
            observed=now + timedelta(minutes=4, seconds=2),
            low=99, high=106, close=103,
        )
        result = price_result(
            market, asset="BTC", horizon_minutes=5, goal=goal,
            now=now + timedelta(minutes=4, seconds=3),
        )
        assert result.status in ("WATCH", "WAIT")
        assert result.hit_at is None
        assert result.last_price == 103
    finally:
        store.close()


def test_a_fully_future_candle_high_or_low_can_confirm_hit(tmp_path):
    now = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
    store, market = _seed(tmp_path, now)
    try:
        up = create_price_goal(
            market, asset="BTC", horizon_minutes=15, target_price=105, now=now,
        )
        down = create_price_goal(
            market, asset="BTC", horizon_minutes=15, target_price=95, now=now,
        )
        _bar(
            market, opened=now + timedelta(seconds=10),
            closed=now + timedelta(minutes=5),
            observed=now + timedelta(minutes=5, seconds=5),
            low=94, high=106, close=100,
        )
        early = price_result(
            market, asset="BTC", horizon_minutes=15, goal=up,
            now=now + timedelta(minutes=4),
        )
        assert early.status == "WATCH"
        late_up = price_result(
            market, asset="BTC", horizon_minutes=15, goal=up,
            now=now + timedelta(minutes=6),
        )
        late_down = price_result(
            market, asset="BTC", horizon_minutes=15, goal=down,
            now=now + timedelta(minutes=6),
        )
        assert late_up.status == "HIT"
        assert late_down.status == "HIT"
        assert late_up.hit_at == now + timedelta(minutes=5)
        assert late_up.fingerprint != early.fingerprint
        assert "105.00" in price_embed(late_up).fields[0].value
    finally:
        store.close()


def test_no_collector_no_target_setup_and_no_fake_hit(tmp_path):
    now = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
    store, market = _seed(tmp_path, now)
    try:
        with pytest.raises(ValueError, match="Live market data"):
            create_price_goal(
                None, asset="BTC", horizon_minutes=5, target_price=105, now=now,
            )
        market.record_feed_health(
            venue="gate", asset="BTC", status="timeout",
            attempted_at=now, new_candles=0, total_candles=0,
        )
        with pytest.raises(ValueError, match="No recent verified"):
            create_price_goal(
                market, asset="BTC", horizon_minutes=5,
                target_price=105, now=now,
            )
        old_goal = PriceGoal("105", now, "gate", "BTC_USDT", "USDT", 100)
        assert price_result(
            market, asset="BTC", horizon_minutes=5,
            goal=old_goal, now=now,
        ).status == "WAIT"
        assert price_result(
            None, asset="BTC", horizon_minutes=5,
            goal=old_goal, now=now,
        ).status == "WAIT"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_price_watch_persists_restarts_and_retargs_without_extra_dm(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store, market = _seed(tmp_path, now)
    dm = DummyDM()
    user = DummyUser(dm)
    service = DirectionTargetService(
        bot=DummyBot(dm), market=market, store=DirectionTargetStore(store),
    )
    try:
        ok, message = await service.follow(
            user, asset="BTC", horizon_minutes=15, target_price=105,
        )
        assert ok and "105" in message
        assert dm.sent == 1
        card = dm.messages[101]
        assert "WATCH" in card.embed.title
        read = DirectionTargetStore(store).get(str(user.id))
        assert read.price_goal is not None
        assert read.price_goal.price == "105"
        assert read.price_goal.reference_price == 100
        resumed = DirectionTargetService(
            bot=DummyBot(dm), market=market, store=DirectionTargetStore(store),
        )
        assert await resumed.cycle() == 0
        ok, _ = await resumed.follow(
            user, asset="BTC", horizon_minutes=5, target_price=106,
        )
        assert ok
        assert dm.sent == 1
        assert resumed.store.get(str(user.id)).price_goal.price == "106"
        ok, _ = await resumed.follow(user, asset="BTC", horizon_minutes=5)
        assert ok and dm.sent == 1
        assert resumed.store.get(str(user.id)).price_goal is None
        assert await resumed.stop(user.id)
        assert resumed.store.active() == []
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_price_goal_settings",
            ).fetchone()["n"] == 0
    finally:
        store.close()


def test_target_goal_schema_is_additive_and_sqlite_idempotent(tmp_path):
    store = SQLiteHotStore(tmp_path / "additive.sqlite")
    targets = DirectionTargetStore(store)
    try:
        targets.initialize()
        targets.initialize()
        with store.connect() as conn:
            assert "rival_direction_targets" in {
                x["name"] for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall()
            }
            assert "rival_price_goal_settings" in {
                x["name"] for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall()
            }
    finally:
        store.close()


def test_postgres_price_goal_migration_persists_and_retargets_atomically():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres integration database unavailable")
    store = PostgresHotStore(dsn)
    watcher = DirectionTargetStore(store)
    user_id = "983000005"
    now = datetime.now(UTC).replace(microsecond=0)
    first = PriceGoal("105", now, "gate", "BTC_USDT", "USDT", 100.0)
    try:
        watcher.initialize()
        watcher.save(
            DirectionTarget(user_id, "990001", "990002", "BTC", 15, "old", first),
        )
        reopened = DirectionTargetStore(PostgresHotStore(dsn))
        restored = reopened.get(user_id)
        assert restored is not None and restored.price_goal == first
        assert len([x for x in reopened.active() if x.user_id == user_id]) == 1

        # Retargeting preserves the original Discord message and does not
        # append a second active user subscription.
        second = PriceGoal("90", now, "gate", "BTC_USDT", "USDT", 100.0)
        reopened.save(
            DirectionTarget(user_id, "990001", "990002", "BTC", 5, "new", second),
        )
        assert reopened.get(user_id).price_goal == second
        reopened.stop(user_id, message_id="wrong-old-message")
        assert reopened.get(user_id) is not None
        reopened.stop(user_id)
        assert reopened.get(user_id) is None
        with reopened.store.connect() as conn:
            assert conn.execute(
                "SELECT 1 FROM rival_price_goal_settings WHERE user_id=?",
                (user_id,),
            ).fetchone() is None
    finally:
        watcher.stop(user_id)
        store.close()


@pytest.mark.asyncio
async def test_slash_command_keeps_direction_mode_and_adds_optional_price(tmp_path):
    client = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "slash.sqlite"), discord_token=None,
    ))
    try:
        cmd = client.tree.get_command("rival-target")
        assert cmd is not None
        assert [p.name for p in cmd.parameters] == [
            "coin", "timeframe", "target_price",
        ]
        assert cmd.parameters[-1].required is False
        assert client.tree.get_command("rival-sports") is not None
        assert client.tree.get_command("rival-stop") is not None
    finally:
        await client.zip_resolver.aclose()
        client.store.close()
