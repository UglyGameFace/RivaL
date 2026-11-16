"""Visible calls remain separate from verified trade recommendations."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.discord_app import call_board
from parlay_bot.discord_app.call_board import (
    CallBoardService,
    build_kalshi_calls,
    build_polymarket_calls,
    live_calls_embed,
)
from parlay_bot.discord_app.targeting import TargetSnapshot
from parlay_bot.storage.hot import SQLiteHotStore


def _snapshot(h, now, *, direction="UP", p=0.62, status="unqualified_model",
              qualified=False):
    if direction == "WAIT":
        return TargetSnapshot(
            asset="BTC", horizon_minutes=h, direction="WAIT",
            detail="No verified prospective prediction", status="no_forecast",
        )
    return TargetSnapshot(
        asset="BTC", horizon_minutes=h, direction=direction,
        detail="Raw research only", venue="hyperliquid", probability=p,
        price=80123.56, issued_at=now-timedelta(seconds=30),
        ends_at=now+timedelta(minutes=h),
        status=status, evidence_qualified=qualified,
        model_version="flow_v3",
    )


def test_call_board_shows_unqualified_up_down_and_only_issued_next(monkeypatch):
    now = datetime(2026, 11, 16, 3, tzinfo=UTC)
    snapshots = {
        5: _snapshot(5, now, direction="DOWN", p=.53, status="weak_model"),
        15: _snapshot(15, now, direction="UP", p=.67),
        60: _snapshot(60, now, direction="WAIT"),
    }
    monkeypatch.setattr(call_board, "target_snapshot",
                        lambda _market, *, asset, horizon_minutes, now:
                        snapshots[horizon_minutes])
    monkeypatch.setattr(call_board, "recent_watches",
                        lambda _market, **_kw: [
                            {"horizon_minutes": 15, "venue": "hyperliquid",
                             "issued": now-timedelta(seconds=25),
                             "target": now+timedelta(minutes=25),
                             "probability_up": .64},
                            {"horizon_minutes": 15, "venue": "hyperliquid",
                             "issued": now+timedelta(seconds=20),
                             "target": now+timedelta(minutes=20),
                             "probability_up": .90},
                            {"horizon_minutes": 5, "venue": "wrong-source",
                             "issued": now-timedelta(seconds=10),
                             "target": now+timedelta(minutes=20),
                             "probability_up": .85},
                        ])
    card, key = live_calls_embed(object(), "BTC", now=now)
    assert len(key) == 64
    assert [x.name for x in card.fields[:3]] == [
        "5-minute call", "15-minute call", "60-minute call",
    ]
    assert "DOWN" in card.fields[0].value
    assert "UP" in card.fields[1].value
    assert "UNQUALIFIED RESEARCH; NO TRADE CALL" in card.fields[1].value
    assert "Confidence: 67.0%" in card.fields[1].value
    assert "Confidence: 53.0%" in card.fields[0].value
    assert "RAW model estimate; NOT calibrated" in card.fields[1].value
    assert "Model: `flow_v3`" in card.fields[1].value
    assert "Exchange: hyperliquid" in card.fields[1].value
    assert "Next *already-issued* target" in card.fields[1].value
    assert "Confidence: 64.0% (raw, research only)" in card.fields[1].value
    assert "90%" not in card.fields[1].value
    assert "Next issued target: not yet available" in card.fields[0].value
    assert "NO CURRENT FORECAST" in card.fields[2].value
    assert "Not verified" in card.fields[3].value
    assert "No real orders" in card.footer.text


def test_call_board_labels_calibrated_confidence_separately(monkeypatch):
    now = datetime(2026, 11, 16, 3, tzinfo=UTC)
    snapshot = _snapshot(15, now, direction="DOWN", p=.82, qualified=True)
    snapshot = replace(snapshot, calibrated=True)
    monkeypatch.setattr(call_board, "target_snapshot",
                        lambda _m, *, asset, horizon_minutes, now:
                        snapshot if horizon_minutes == 15 else
                        _snapshot(horizon_minutes, now, direction="WAIT"))
    monkeypatch.setattr(call_board, "recent_watches", lambda *_a, **_k: [])
    card, _ = live_calls_embed(object(), "BTC", now=now)
    assert "Confidence: 82.0%" in card.fields[1].value
    assert "Platt-calibrated model estimate" in card.fields[1].value
    assert "forward reliability unverified" in card.fields[1].value
    assert "WATCH ONLY; profitability not proven" in card.fields[1].value
    assert "RAW model estimate" not in card.fields[1].value


def test_call_board_keeps_all_horizons_as_wait_when_collector_unavailable():
    card, first = live_calls_embed(None, "XRP",
                                   now=datetime(2026, 11, 16, tzinfo=UTC))
    assert sum("NO CURRENT FORECAST" in x.value for x in card.fields) == 3
    assert all("UP" not in x.value for x in card.fields[:3])
    assert len(first) == 64


@pytest.mark.asyncio
async def test_live_calls_are_opt_in_channel_posts_and_edit_in_place(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "calls.sqlite")
    try:
        state = {"hash": "a"}
        def render(_market, asset):
            return discord.Embed(title=f"Live {asset}"), state["hash"]
        monkeypatch.setattr(call_board, "live_calls_embed", render)
        original = SimpleNamespace(id=321, edit=AsyncMock())
        sent = SimpleNamespace(id=321, delete=AsyncMock())
        channel = SimpleNamespace(
            id=777, send=AsyncMock(return_value=sent),
            get_partial_message=lambda _id: original,
        )
        bot = SimpleNamespace(get_partial_messageable=lambda _id: channel)
        service = CallBoardService(bot, store, object())
        interaction = SimpleNamespace(
            channel=channel, guild_id=12345, user=SimpleNamespace(id=42),
        )
        result = await service.start(interaction, "BTC")
        assert "One message updates automatically" in result
        assert len(service._rows()) == 1
        assert channel.send.await_count == 1
        assert "already has" in await service.start(interaction, "BTC")
        assert channel.send.await_count == 1

        assert await service.cycle() == 0
        state["hash"] = "b"
        # The live issuer calls request_refresh; no 60-second wait needed.
        assert await service.cycle(force=True) == 1
        assert original.edit.await_count == 1
        assert service._rows()[0]["fingerprint"] == "b"
        assert await service.stop_owner(999) is False
        assert len(service._rows()) == 1
        assert await service.stop_owner(42) is True
        assert not service._rows()
        assert original.edit.await_count == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_calls_reject_dms_and_are_revoked_by_stop_all(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "calls-stop.sqlite")
    try:
        monkeypatch.setattr(call_board, "live_calls_embed",
                            lambda _m, asset: (discord.Embed(title=asset), "hash"))
        original = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(
            id=777, send=AsyncMock(return_value=SimpleNamespace(id=321)),
            get_partial_message=lambda _id: original,
        )
        service = CallBoardService(
            SimpleNamespace(get_partial_messageable=lambda _id: channel),
            store, object(),
        )
        user = SimpleNamespace(id=42)
        dm = SimpleNamespace(channel=channel, guild_id=None, user=user)
        assert "No DM" in await service.start(dm, "ETH")
        channel.send.assert_not_awaited()
        await service.start(SimpleNamespace(
            channel=channel, guild_id=111, user=user,
        ), "ETH")
        from parlay_bot.discord_app.alert_control import stop_all_notifications
        message = await stop_all_notifications(
            42, auto_hunt=None, targets=None, tracker=None, calls=service,
        )
        assert "Stopped all" in message
        assert not service._rows()
    finally:
        store.close()

@pytest.mark.asyncio
async def test_polymarket_uses_actual_current_next_contract_and_frozen_confidence(monkeypatch):
    from parlay_bot.discord_app import polymarket_live_board

    now = datetime.now(UTC)
    call = SimpleNamespace(
        direction="DOWN", probability_up=.21, model="contract_calib",
        issued_at=now-timedelta(minutes=1),
    )
    obs = SimpleNamespace(
        _observation=AsyncMock(return_value=(
            discord.Embed(title="Actual Polymarket BTC 5m",
                          description="Current and NEXT listed contract"), "existing",
        )),
        peek=lambda asset, horizon: (object(), call),
    )
    monkeypatch.setattr(polymarket_live_board, "matching_call",
                        lambda *a, **kw: True)
    card, digest = await build_polymarket_calls(obs, "BTC", 5)
    assert "Polymarket" in card.title
    assert "NEXT listed contract" in card.description
    assert "DOWN · 79.0%" in card.fields[-1].value
    assert "raw contract-model estimate" in card.fields[-1].value
    assert "contract_calib" in card.fields[-1].value
    assert len(digest) == 64
    monkeypatch.setattr(polymarket_live_board, "matching_call",
                        lambda *a, **kw: False)
    card, _ = await build_polymarket_calls(obs, "BTC", 5)
    assert "No verified matching" in card.fields[-1].value
    assert "79.0%" not in card.fields[-1].value


@pytest.mark.asyncio
async def test_kalshi_uses_exact_listed_contract_rules_not_exchange_candle():
    clock = datetime.now(UTC)
    markets = (
        SimpleNamespace(ticker="BTC-EXACT-A", title="Bitcoin above $100k?",
                        close_time=clock+timedelta(hours=1)),
        SimpleNamespace(ticker="BTC-EXACT-B", title="Bitcoin below $90k?",
                        close_time=clock+timedelta(hours=2)),
        SimpleNamespace(ticker="BTC-EXACT-C", title="Unobserved crypto?",
                        close_time=clock+timedelta(hours=3)),
    )
    browser = SimpleNamespace(browse=AsyncMock(
        return_value=SimpleNamespace(markets=markets),
    ))
    def preview(*, asset, market):
        if market.ticker == "BTC-EXACT-C":
            return None
        return {"model": "kalshi_shad_v1",
                "candidate_yes": .76 if market.ticker.endswith("A") else .31}
    reader = SimpleNamespace(contract_preview=preview)
    card, digest = await build_kalshi_calls(browser, reader, "BTC")
    assert "Kalshi" in card.title
    assert "YES · Confidence: 76.0%" in card.fields[0].value
    assert "NO · Confidence: 69.0%" in card.fields[1].value
    assert "NO ISSUED MODEL CALL" in card.fields[2].value
    assert "5m" not in card.fields[0].value
    assert len(digest) == 64


@pytest.mark.asyncio
async def test_new_exchange_issuance_refreshes_immediately_without_network_burst(tmp_path):
    store = SQLiteHotStore(tmp_path / "fast-calls.sqlite")
    try:
        service = CallBoardService(SimpleNamespace(), store, object())
        service.initialize()
        service._save(100, "exchange", "BTC", 0, 101, 7, "old")
        service._save(100, "polymarket", "BTC", 5, 102, 7, "old")
        service._last_evaluated = {
            ("exchange", "BTC", 0): datetime.now(UTC),
            ("polymarket", "BTC", 5): datetime.now(UTC),
        }
        service._last_revision[("exchange", "BTC", 0)] = "original"
        seen = []
        async def render(kind, asset, horizon):
            seen.append((kind, asset, horizon))
            return discord.Embed(title=kind), "updated"
        service._render = render
        service._revision = lambda asset: "new-forecast"
        service._edit = AsyncMock(return_value=True)
        assert await service.cycle() == 1
        assert seen == [("exchange", "BTC", 0)]
        assert await service.cycle() == 0  # unchanged revisions never spam
        service.request_refresh()
        assert await service.wait_for_refresh() is True
        assert await service.cycle(force=True) == 1
        assert seen[-1] == ("exchange", "BTC", 0)
        assert all(kind == "exchange" for kind, _, _ in seen)
        # A five-minute target resolves even when the source issues nothing
        # new. Refresh within one short check, never leave an old UP card.
        scope = ("exchange", "BTC", 0)
        service._last_boundary[scope] = int(datetime.now(UTC).timestamp()) // 300 - 1
        assert await service.cycle() == 1
        assert seen[-1] == scope
    finally:
        store.close()


@pytest.mark.asyncio
async def test_market_and_horizon_are_persisted_separately(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "market-identity.sqlite")
    try:
        service = CallBoardService(SimpleNamespace(), store, object())
        async def render(kind, asset, horizon):
            return discord.Embed(title=f"{kind}/{horizon}"), f"{kind}/{horizon}"
        service._render = render
        ids = iter((101, 102, 103))
        channel = SimpleNamespace(id=777, send=AsyncMock(
            side_effect=lambda **kw: SimpleNamespace(id=next(ids)),
        ))
        actor = SimpleNamespace(channel=channel, guild_id=1,
                                user=SimpleNamespace(id=42))
        for kind, horizon in (
            ("exchange", 5), ("polymarket", 5), ("polymarket", 15),
        ):
            assert "Posted" in await service.start(
                actor, "BTC", kind=kind, horizon=horizon,
            )
        rows = service._rows()
        assert {(r["market_kind"], r["horizon_minutes"]) for r in rows} == {
            ("exchange", 0), ("polymarket", 5), ("polymarket", 15),
        }
        assert "No source-matched" in await service.start(
            actor, "BTC", kind="draftkings",
        )
    finally:
        store.close()

@pytest.mark.asyncio
async def test_slow_contract_market_does_not_delay_new_exchange_call(tmp_path):
    store = SQLiteHotStore(tmp_path / "independent-markets.sqlite")
    try:
        service = CallBoardService(SimpleNamespace(), store, object())
        service.initialize()
        service._save(1, "exchange", "BTC", 0, 101, 42, "old")
        service._save(1, "polymarket", "BTC", 5, 102, 42, "old")
        poly_started = asyncio.Event()
        release_poly = asyncio.Event()
        exchange_delivered = asyncio.Event()
        edits = []

        async def render(kind, asset, horizon):
            if kind == "polymarket":
                poly_started.set()
                await release_poly.wait()
            return discord.Embed(title=kind), f"{kind}:new"

        async def edit(row, _card, _digest):
            edits.append(row["market_kind"])
            if row["market_kind"] == "exchange":
                exchange_delivered.set()
            return True

        service._render = render
        service._edit = edit
        running = asyncio.create_task(service.cycle())
        try:
            await asyncio.wait_for(poly_started.wait(), timeout=2)
            await asyncio.wait_for(exchange_delivered.wait(), timeout=2)
            assert not running.done()
            assert edits == ["exchange"]
        finally:
            release_poly.set()
        assert await asyncio.wait_for(running, timeout=2) == 2
        assert set(edits) == {"exchange", "polymarket"}
    finally:
        store.close()
