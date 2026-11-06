"""One immutable first forecast per contract and one recoverable live DM panel."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.discord_app.polymarket_live_board import (
    LivePolymarketBoardService,
    LivePolymarketBoardStore,
    LivePolymarketTracker,
    live_polymarket_embed,
)
from parlay_bot.discord_app.polymarket_watch import (
    PolymarketListedWindow,
    PolymarketNotice,
    PolymarketWindowBoard,
)
from parlay_bot.perpetuals import polymarket_learning
from parlay_bot.perpetuals.model_checkpoints import canonical
from parlay_bot.perpetuals.polymarket_learning import MODEL
from parlay_bot.perpetuals.polymarket_research import ContractCall, PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore


def _notice() -> PolymarketNotice:
    now = datetime.now(UTC)
    start = now - timedelta(minutes=1)
    return PolymarketNotice(
        asset="BTC", horizon=5, issued=start,
        expires=now + timedelta(minutes=4),
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        up_ask=.52, down_ask=.49, quote_at=now,
        condition_id="0x" + "1" * 64,
        up_token_id="1" * 60, down_token_id="2" * 60,
        resolution_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
        rules="Chainlink TWAP 60 seconds",
    )


def test_book_quote_and_new_weights_cannot_flip_the_first_saved_forecast(tmp_path, monkeypatch):
    hot = SQLiteHotStore(tmp_path / "first.sqlite")
    hot.initialize()
    research = PolymarketResearch(hot)
    research.initialize()  # Creates the shared terminal-outcome receipt table.
    learner = research.learner
    # First issuance with zero-valued weights is neutral. Then simulate
    # a newly trained model and an enormous live price shift.
    monkeypatch.setattr(polymarket_learning, "MIN_LABELS", 0)
    notice = _notice()
    try:
        first = learner.observe(notice)
        assert first is not None
        assert first["direction"] == "WAIT"
        assert first["probability_up"] == .5
        weights = canonical([0.0, 10.0, -10.0] + [0.0] * 8)
        weight_sha = hashlib.sha256(weights.encode()).hexdigest()
        with hot.connect() as conn:
            conn.execute(
                "UPDATE rival_polymarket_model_state SET weights_json=?,"
                "weights_sha256=?,updates=100 WHERE model_version=?",
                (weights, weight_sha, MODEL),
            )
        later = replace(
            notice, quote_at=datetime.now(UTC), up_ask=.95, down_ask=.06,
        )
        second = learner.observe(later)
        assert second is not None
        for key in ("direction", "probability_up", "issued_at",
                    "weights_sha256", "evidence_sha256"):
            assert second[key] == first[key]
        with hot.connect() as conn:
            rows = conn.execute(
                "SELECT probability_up,issued_at FROM rival_polymarket_model_forward "
                "WHERE slug=? AND model_version=?", (notice.slug, MODEL),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["issued_at"] == first["issued_at"].isoformat()
    finally:
        hot.close()


def test_first_model_call_keeps_priority_over_a_later_opposite_ai_review(tmp_path):
    hot = SQLiteHotStore(tmp_path / "calls.sqlite")
    research = PolymarketResearch(hot)
    now = datetime.now(UTC)
    close = now + timedelta(minutes=3)
    initial = ContractCall(
        slug="s", asset="BTC", horizon=5, issued_at=now - timedelta(minutes=2),
        expires=close, direction="DOWN", model=MODEL, thesis="first",
        risk="research", evidence_sha256="a" * 64, kind="model",
    )
    later = replace(initial, direction="UP", issued_at=now, kind="AI")
    try:
        research._math_calls["s"] = initial
        research._calls["s"] = later
        assert research.peek("s") is initial
    finally:
        hot.close()


def test_optional_ai_review_also_keeps_the_first_saved_direction(tmp_path):
    hot = SQLiteHotStore(tmp_path / "ai.sqlite")
    hot.initialize()
    research = PolymarketResearch(hot)
    notice = _notice()
    research.initialize()
    first = ContractCall(
        slug=notice.slug, asset="BTC", horizon=5,
        issued_at=notice.issued + timedelta(seconds=10),
        expires=notice.expires, direction="DOWN", model="independent-review",
        thesis="initial research", risk="not proven",
        evidence_sha256="a" * 64,
    )
    second = replace(
        first, direction="UP",
        issued_at=first.issued_at + timedelta(seconds=30),
        evidence_sha256="b" * 64,
    )
    try:
        assert research._save(first, notice, {"up_ask": .4}).direction == "DOWN"
        assert research._save(second, notice, {"up_ask": .6}).direction == "DOWN"
        assert research._saved(notice.slug).issued_at == first.issued_at
    finally:
        hot.close()


@pytest.mark.asyncio
async def test_quote_refreshes_edit_only_one_dm_and_send_no_flip_alerts(tmp_path, monkeypatch):
    hot = SQLiteHotStore(tmp_path / "panel.sqlite")
    hot.initialize()
    store = LivePolymarketBoardStore(hot)
    store.initialize()
    edited = AsyncMock()
    channel = SimpleNamespace(
        send=AsyncMock(),
        get_partial_message=lambda _message_id: SimpleNamespace(edit=edited),
    )
    bot = SimpleNamespace(get_partial_messageable=lambda _channel_id: channel)
    service = LivePolymarketBoardService(
        bot=bot, store=store, reader=SimpleNamespace(), market=None,
    )
    store.save(LivePolymarketTracker("123", "BTC", 5, "555", "901", "old"))
    monkeypatch.setattr(service, "_observation", AsyncMock(side_effect=[
        (discord.Embed(title="BTC first quote"), "first"),
        (discord.Embed(title="BTC second quote"), "second"),
    ]))
    try:
        assert await service.cycle() == 1
        assert await service.cycle() == 1
        assert edited.await_count == 2
        channel.send.assert_not_awaited()
        assert store.get("123").message_id == "901"
        assert store.get("123").fingerprint == "second"
    finally:
        await service.aclose()
        hot.close()


def test_live_panel_labels_frozen_call_and_keeps_quotes_separate():
    now = datetime.now(UTC)
    window = PolymarketListedWindow(
        asset="BTC", horizon=5, slug="btc-updown-5m-panel",
        opens=now - timedelta(minutes=1), closes=now + timedelta(minutes=4),
        accepting_orders=True, price_to_beat=None,
    )
    notice = replace(_notice(), slug=window.slug, issued=window.opens,
                     expires=window.closes, up_ask=.07, down_ask=.94)
    board = PolymarketWindowBoard(
        checked_at=now, current=window, next_window=None, current_quote=notice,
    )
    call = ContractCall(
        slug=window.slug, asset="BTC", horizon=5,
        issued_at=now - timedelta(seconds=40), expires=window.closes,
        direction="DOWN", model=MODEL, thesis="Frozen first estimate",
        risk="research", evidence_sha256="a" * 64, kind="model",
    )
    embed = live_polymarket_embed(board, call, asset="BTC", horizon=5)
    fields = {field.name: field.value for field in embed.fields}
    frozen = [value for name, value in fields.items()
              if "First model call (frozen)" in name]
    assert len(frozen) == 1
    assert "DOWN" in frozen[0]
    assert "UP $0.07" in fields["💰 Polymarket share asks"]
    assert "DOWN $0.94" in fields["💰 Polymarket share asks"]
    assert all("Call changed" not in name for name in fields)
    assert "First model call frozen" in embed.footer.text
