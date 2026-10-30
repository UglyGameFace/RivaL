"""Auto-Hunt acceptance checks: opt-in, qualification, restart dedupe, stop."""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app import auto_hunt
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def hunt_store(tmp_path):
    hot = SQLiteHotStore(tmp_path / "rival-hunt.sqlite")
    hot.initialize()
    store = auto_hunt.AutoHuntStore(hot)
    store.initialize()
    return store


def candidate(now=None, **replace):
    now = now or datetime.now(UTC)
    args = {
        "asset": "BTC", "horizon": 5, "venue": "binance",
        "issued": now - timedelta(seconds=10),
        "expires": now + timedelta(minutes=3),
        "probability_up": 0.64, "bid": 100.0, "ask": 100.1,
        "quote_at": now, "engine": "live", "model_version": "forecast-v1",
    }
    args.update(replace)
    return auto_hunt.HuntCandidate(**args)


def test_auto_hunt_requires_opt_in_and_claims_once_across_restarts(hunt_store):
    now = datetime.now(UTC)
    watch = candidate(now)
    assert hunt_store.subscribers() == []
    assert not hunt_store.claim("1001", watch, now=now)
    assert hunt_store.subscribe("1001", now=now - timedelta(seconds=20))
    assert not hunt_store.subscribe("1001")
    assert hunt_store.claim("1001", watch, now=now)
    restarted = auto_hunt.AutoHuntStore(hunt_store.store)
    restarted.initialize()
    assert not restarted.claim("1001", watch, now=now)
    restarted.finish("1001", watch, sent=True, message_id="555")
    assert restarted.enabled("1001")
    assert restarted.stop("1001")
    assert restarted.subscribers() == []
    assert not restarted.claim("1001", candidate(now, issued=now), now=now)
    assert restarted.subscribe("1001", now=now)
    # Reopt-in cannot re-trigger a previously delivered forecast.
    assert not restarted.claim("1001", watch, now=now)


def test_hunt_claim_blocks_pre_opt_in_and_cooldown(hunt_store):
    now = datetime.now(UTC)
    assert hunt_store.subscribe("1001", now=now)
    assert not hunt_store.claim("1001", candidate(now), now=now)
    future = candidate(now, issued=now, model_version="forecast-v2")
    assert hunt_store.claim("1001", future, now=now)
    later = candidate(now, issued=now, model_version="forecast-v3")
    assert not hunt_store.claim(
        "1001", later, now=now + timedelta(seconds=30),
    )
    hunt_store.finish("1001", future, sent=False)
    # An ambiguous send failure must NOT be retried after a restart.
    assert not hunt_store.claim("1001", future, now=now + timedelta(minutes=3))


def test_hunt_user_bound_review_receipts(hunt_store):
    now = datetime.now(UTC)
    hunt_store.subscribe("1001", now=now - timedelta(minutes=1))
    watch = candidate(now)
    assert hunt_store.claim("1001", watch, now=now)
    hunt_store.finish("1001", watch, sent=True, message_id="555")
    service = auto_hunt.AutoHuntService(
        bot=SimpleNamespace(), market=object(), quotes=object(), store=hunt_store,
    )
    assert service.delivery_for_message("2002", "555") is None
    assert service.delivery_for_message("1001", "555")["venue"] == "binance"
    assert not hunt_store.reviewed("2002", "555")
    assert hunt_store.reviewed("1001", "555")


def _mock_watch(now, *, probability=0.64, venue="binance", issued_delta=5):
    return {
        "venue": venue, "asset": "BTC", "horizon_minutes": 5,
        "issued": now - timedelta(seconds=issued_delta),
        "target": now + timedelta(minutes=3),
        "probability_up": probability, "price": 100.0,
    }


def test_hunt_forecasts_require_fresh_matching_quote_and_true_evidence(monkeypatch):
    now = datetime.now(UTC)
    forecasts = [_mock_watch(now)]
    monkeypatch.setattr(auto_hunt, "promoted_engine", lambda *a, **k: "live")
    monkeypatch.setattr(
        auto_hunt, "recent_watches",
        lambda *a, asset, **k: forecasts if asset == "BTC" else [],
    )
    proofs = {"model_beats_baseline": True, "settled": 60}
    monkeypatch.setattr(auto_hunt, "_venue_proof", lambda *a, **k: proofs)
    book = SimpleNamespace(
        bid=100, ask=100.1, exchange_at=now,
        validate=lambda *, now: None,
    )
    fetch = AsyncMock(return_value=book)
    monkeypatch.setattr(auto_hunt, "_venue_book", fetch)
    market = SimpleNamespace(store=object())
    picks = run(auto_hunt.eligible_research_watches(market, object(), now=now))
    assert len(picks) == 1
    assert picks[0].asset == "BTC" and picks[0].venue == "binance"
    assert "NOT" in auto_hunt.hunt_embed(picks[0], observed_at=now).description
    proofs["model_beats_baseline"] = None
    assert run(auto_hunt.eligible_research_watches(market, object(), now=now)) == []
    proofs["model_beats_baseline"] = False
    assert run(auto_hunt.eligible_research_watches(market, object(), now=now)) == []
    proofs["model_beats_baseline"] = True
    forecasts[:] = [_mock_watch(now, probability=0.52)]
    assert run(auto_hunt.eligible_research_watches(market, object(), now=now)) == []
    forecasts[:] = [_mock_watch(now, issued_delta=240)]
    assert run(auto_hunt.eligible_research_watches(market, object(), now=now)) == []
    forecasts[:] = [_mock_watch(now)]
    # A market with 100bps spread is not qualified even if model scores pass.
    book.ask = 101
    assert run(auto_hunt.eligible_research_watches(market, object(), now=now)) == []


def test_hunt_sends_once_only_after_opt_in_and_respects_stop(hunt_store, monkeypatch):
    now = datetime.now(UTC)
    watch = candidate(now)
    monkeypatch.setattr(
        auto_hunt, "new_forecast_watches",
        AsyncMock(return_value=[watch]),
    )
    channel = SimpleNamespace(
        id=999,
        send=AsyncMock(return_value=SimpleNamespace(id=555)),
    )
    user = SimpleNamespace(dm_channel=channel, create_dm=AsyncMock(return_value=channel))
    bot = SimpleNamespace(
        get_user=lambda user_id: user,
        fetch_user=AsyncMock(return_value=user),
    )
    service = auto_hunt.AutoHuntService(
        bot=bot, market=object(), quotes=object(), store=hunt_store,
    )

    assert run(service.cycle()) == 0
    assert channel.send.await_count == 0
    hunt_store.subscribe("1001", now=now - timedelta(seconds=60))
    assert run(service.cycle()) == 1
    assert run(service.cycle()) == 0
    assert channel.send.await_count == 1
    assert hunt_store.stop("1001")
    assert run(service.cycle()) == 0
    assert channel.send.await_count == 1


def test_auto_hunt_is_no_wager_and_has_restart_safe_button_ids():
    view = auto_hunt.AutoHuntActions()
    assert view.is_persistent()
    assert {item.custom_id for item in view.children} == {
        "rival:hunt:review", "rival:hunt:dismiss",
    }
    from pathlib import Path
    text = Path(auto_hunt.__file__).read_text()
    assert ".place_order(" not in text
    assert "submit_bet(" not in text


def test_postgres_auto_hunt_subscription_claim_and_stop_survive_restart():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL16 integration database unavailable")
    hot = PostgresHotStore(dsn)
    store = auto_hunt.AutoHuntStore(hot)
    user_id = "990000000009900001"
    now = datetime.now(UTC)
    watch = candidate(now)
    try:
        store.initialize()
        store.stop(user_id)
        with hot.connect() as conn:
            conn.execute("DELETE FROM rival_hunt_delivery WHERE user_id=?", (user_id,))
        assert store.subscribe(user_id, now=now - timedelta(minutes=1))
        assert store.claim(user_id, watch, now=now)
        resumed = auto_hunt.AutoHuntStore(hot)
        resumed.initialize()
        assert not resumed.claim(user_id, watch, now=now + timedelta(seconds=3))
        resumed.finish(user_id, watch, sent=True, message_id="99988")
        assert resumed.reviewed(user_id, "99988")
        assert resumed.stop(user_id)
        assert resumed.subscribers() == []
    finally:
        with hot.connect() as conn:
            conn.execute("DELETE FROM rival_hunt_delivery WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM rival_hunt_subscriptions WHERE user_id=?", (user_id,))
        hot.close()


def test_a_slow_other_user_does_not_block_alert_cancellation(hunt_store):
    hunt_store.subscribe("1001")
    hunt_store.subscribe("1002")
    service = auto_hunt.AutoHuntService(
        bot=SimpleNamespace(), market=object(), quotes=object(),
        store=hunt_store,
    )

    async def exercise():
        async with service._user_lock("1001"):
            return await asyncio.wait_for(service.stop(1002), timeout=1.0)

    assert run(exercise())
    assert hunt_store.enabled("1001")
    assert not hunt_store.enabled("1002")


def test_two_opted_in_members_receive_concurrent_bounded_alerts(hunt_store, monkeypatch):
    now = datetime.now(UTC)
    hunt_store.subscribe("1001", now=now - timedelta(minutes=1))
    hunt_store.subscribe("1002", now=now - timedelta(minutes=1))
    monkeypatch.setattr(
        auto_hunt, "new_forecast_watches",
        AsyncMock(return_value=[candidate(now)]),
    )
    simultaneous = 0
    peak = 0
    both_sending = asyncio.Event()

    async def send(**kwargs):
        nonlocal simultaneous, peak
        simultaneous += 1
        peak = max(peak, simultaneous)
        if simultaneous == 2:
            both_sending.set()
        # Assert actual concurrent delivery rather than relying on a 30 ms
        # scheduler window under variable PostgreSQL/CI worker load.
        await asyncio.wait_for(both_sending.wait(), timeout=3)
        simultaneous -= 1
        return SimpleNamespace(id=1000 + peak)

    channel = SimpleNamespace(id=123, send=AsyncMock(side_effect=send))
    user = SimpleNamespace(dm_channel=channel)
    client = SimpleNamespace(
        get_user=lambda _uid: user,
        fetch_user=AsyncMock(return_value=user),
    )
    service = auto_hunt.AutoHuntService(
        bot=client, market=object(), quotes=object(), store=hunt_store,
    )
    assert run(service.cycle()) == 2
    assert peak == 2
    assert channel.send.await_count == 2


def test_new_forecast_alerts_not_blocked_by_unproven_model_edge(monkeypatch):
    """A valid raw forecast can notify without pretending it is tradable."""
    now = datetime.now(UTC)
    monkeypatch.setattr(auto_hunt, "promoted_engine", lambda *a, **k: "live")
    watch = _mock_watch(now, probability=0.54)
    monkeypatch.setattr(
        auto_hunt, "recent_watches",
        lambda *a, asset, **k: [watch] if asset == "BTC" else [],
    )
    monkeypatch.setattr(
        auto_hunt, "_venue_proof",
        lambda *a, **k: {"model_beats_baseline": False},
    )
    monkeypatch.setattr(
        auto_hunt, "_venue_book", AsyncMock(side_effect=AssertionError(
            "Forecast notification must not call external book API",
        )),
    )
    market = SimpleNamespace(store=object())
    forecasts = run(auto_hunt.new_forecast_watches(market, now=now))
    assert len(forecasts) == 1
    found = forecasts[0]
    assert (found.asset, found.horizon, found.direction) == ("BTC", 5, "UP")
    assert not found.screened and found.bid is None
    embed = auto_hunt.hunt_embed(found, observed_at=now)
    assert "NOT VERIFIED" in embed.description
    assert "not calibrated odds" in embed.description
    assert run(auto_hunt.eligible_research_watches(
        market, object(), now=now,
    )) == []


def test_forecast_alerts_reject_late_or_already_expired(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(auto_hunt, "promoted_engine", lambda *a, **k: "live")
    watches = [_mock_watch(now, issued_delta=8)]
    monkeypatch.setattr(
        auto_hunt, "recent_watches",
        lambda *a, asset, **k: watches if asset == "BTC" else [],
    )
    market = SimpleNamespace(store=object())
    assert len(run(auto_hunt.new_forecast_watches(market, now=now))) == 1
    watches[:] = [_mock_watch(now, issued_delta=250)]
    assert run(auto_hunt.new_forecast_watches(market, now=now)) == []
    watches[:] = [{**_mock_watch(now), "target": now - timedelta(minutes=5)}]
    assert run(auto_hunt.new_forecast_watches(market, now=now)) == []


def test_daily_alert_budgets_separate_research_live_trends_and_sources(hunt_store):
    now = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    user = "budget-100"
    hunt_store.subscribe(user, now=now - timedelta(minutes=5))
    cases = (
        ("research", candidate(now, venue="binance", model_version="research"), None),
        ("qualified", candidate(now, venue="hyperliquid", model_version="qualified"),
         "qualified|confirmed"),
        ("polymarket", candidate(now, venue="polymarket", model_version="poly"), None),
        ("flip", candidate(now, venue="okx", model_version="flip"),
         "flip|reversal"),
        ("live_shift", candidate(now, venue="live_trend", model_version="shift"),
         None),
    )
    for tier, watch, key in cases:
        assert hunt_store.claim(
            user, watch, now=now, key=key, apply_cooldown=False,
        ), tier
        hunt_store.finish(
            user, watch, key=key, sent=True,
            message_id=f"test-{tier}",
        )
    counts = hunt_store.daily_sent_counts(user, now=now)
    assert counts == {
        "research": 1, "qualified": 1, "polymarket": 1,
        "flip": 1, "live_shift": 1,
    }
    for tier in counts:
        assert hunt_store.count_sent_today(user, tier, now) == 1
    assert all(
        n == 0 for n in hunt_store.daily_sent_counts(
            user, now=now + timedelta(days=1),
        ).values()
    )


def test_alert_status_explains_daily_limits_and_reset(hunt_store):
    user_id = 33331
    hunt_store.subscribe(str(user_id))
    service = auto_hunt.AutoHuntService(
        bot=SimpleNamespace(), market=object(), quotes=object(),
        store=hunt_store,
    )
    result = run(service.status(user_id))
    assert "Today's sent alerts (UTC)" in result
    assert "Research 0/12" in result
    assert "Qualified" not in result
    assert "Resets <t:" in result
    assert "Sent 0 total" in result


def test_capped_subscribers_do_not_trigger_expensive_external_scans(
    hunt_store, monkeypatch,
):
    user = "capped-100"
    hunt_store.subscribe(user, now=datetime.now(UTC) - timedelta(minutes=1))
    hunt_store.set_tier(user, "qualified", True)
    hunt_store.set_tier(user, "polymarket", True)
    monkeypatch.setattr(
        hunt_store, "daily_sent_counts",
        lambda _user, *, now=None: {
            "research": 12, "qualified": 12, "polymarket": 12,
            "flip": 12, "live_shift": 0,
        },
    )
    scans = (AsyncMock(side_effect=AssertionError("network scan under cap")),
             AsyncMock(side_effect=AssertionError("model scan under cap")),
             AsyncMock(side_effect=AssertionError("Polymarket scan under cap")),
             AsyncMock(side_effect=AssertionError("flip scan under cap")))
    monkeypatch.setattr(auto_hunt, "qualified_watches", scans[0])
    monkeypatch.setattr(auto_hunt, "new_forecast_watches", scans[1])
    monkeypatch.setattr(auto_hunt, "direction_change_watches", scans[3])
    service = auto_hunt.AutoHuntService(
        bot=SimpleNamespace(), market=object(), quotes=object(),
        store=hunt_store,
    )
    monkeypatch.setattr(service.polymarket, "active", scans[2])
    assert run(service.cycle()) == 0
    assert all(scan.await_count == 0 for scan in scans)
