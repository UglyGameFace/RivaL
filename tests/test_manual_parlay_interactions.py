"""Regression coverage for private parlay interaction guards and controls."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from parlay_bot.discord_app.manual_parlay import ManualParlayView, _Action, _Filter, _Pick


def quote(fixture, key, price=2.0):
    return {
        "fixture_id": fixture, "selection_key": key,
        "bookmaker": "draftkings", "price_decimal": price,
        "implied_probability": 1 / price,
        "event_name": fixture, "market_name": "Winner",
        "outcome_name": "Home", "market_id": "winner",
        "outcome_id": "home", "sport_name": "Basketball",
        "provider": "oddspapi",
        "observed_at": datetime.now(UTC).isoformat(),
        "start_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


def picker():
    with patch(
        "parlay_bot.discord_app.manual_parlay.available_selections",
        return_value=[quote("game1", "a"), quote("game1", "b"), quote("game2", "c")],
    ):
        return ManualParlayView(owner_id=123, store=MagicMock(), eligible_books={"draftkings"})


@pytest.mark.asyncio
async def test_other_user_cannot_interact():
    view = picker()
    interaction = MagicMock()
    interaction.user.id = 456
    interaction.response.send_message = AsyncMock()
    assert await view.interaction_check(interaction) is False
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_selection_and_remove_buttons():
    view = picker()
    game = next(x for x in view.children if isinstance(x, _Filter) and x.kind == "game")
    game._values = ["game1"]
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    await game.callback(interaction)
    category = next(x for x in view.children if isinstance(x, _Filter) and x.kind == "category")
    category._values = ["Full Game"]
    await category.callback(interaction)
    select = next(x for x in view.children if isinstance(x, _Pick))
    select._values = ["0"]
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    await select.callback(interaction)
    assert len(view.legs) == 1
    assert "50.0%" in view.render().description
    remove = next(x for x in view.children if isinstance(x, _Action) and x.action == "remove")
    await remove.callback(interaction)
    assert view.legs == []


@pytest.mark.asyncio
async def test_same_fixture_selection_is_blocked():
    view = picker()
    view.legs = [quote("game1", "a")]
    view.game = "game1"
    view.category = "Full Game"
    view.refresh_components()
    select = next(x for x in view.children if isinstance(x, _Pick))
    select._values = ["1"]
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    await select.callback(interaction)
    assert len(view.legs) == 1
    interaction.response.send_message.assert_awaited_once()
