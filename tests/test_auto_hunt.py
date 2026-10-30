"""Auto-Hunt acceptance checks: opt-in, qualification, restart dedupe, stop."""
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
        auto_hunt, "eligible_research_watches",
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
