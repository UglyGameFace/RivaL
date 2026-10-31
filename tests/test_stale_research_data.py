"""The two private dashboards recover after restart without replaying spending."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from parlay_bot.discord_app.home import install_view_dispatch_logging
from parlay_bot.discord_app.stale_dashboards import (
    DATA_BUTTONS,
    DATA_SELECTS,
    RESEARCH_BUTTONS,
    RESEARCH_SELECTS,
    build_stale_dashboard_views,
)


class Fresh(discord.ui.View):
    def __init__(self, owner_id: int, *, tag: str):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.tag = tag

    async def render(self):
        return discord.Embed(title=f"Fresh {self.tag}", description="Verified data")


def _views():
    return build_stale_dashboard_views(
        lambda owner: Fresh(owner, tag="research"),
        lambda owner: Fresh(owner, tag="data"),
    )


def _ids(view):
    return {item.custom_id for item in view.children}


def _interaction(owner=42):
    return SimpleNamespace(
        user=SimpleNamespace(id=owner),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def test_recovery_custom_ids_cover_every_live_dashboard_component():
    research, data = _views()
    for scope, view, buttons, selects in (
        ("research", research, RESEARCH_BUTTONS, RESEARCH_SELECTS),
        ("data", data, DATA_BUTTONS, DATA_SELECTS),
    ):
        assert view.timeout is None and view.is_persistent()
        assert len(view.children) <= 25
        assert _ids(view) == {
            f"rival:{scope}:{action}" for action in (*buttons, *selects)
        }
        assert all(item.custom_id for item in view.children)
        assert max(item.row for item in view.children) <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,custom_id", [
    ("research", "rival:research:analyze"),
    ("research", "rival:research:follow-target"),
    ("research", "rival:research:section"),
    ("data", "rival:data:choose:exchange"),
    ("data", "rival:data:refresh"),
])
async def test_stale_controls_open_new_private_panel_not_old_action(scope, custom_id):
    research, data = _views()
    view = research if scope == "research" else data
    item = next(child for child in view.children if child.custom_id == custom_id)
    interaction = _interaction(owner=8123)
    await item.callback(interaction)
    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert isinstance(kwargs["view"], Fresh)
    assert kwargs["view"].owner_id == 8123
    assert "No previous action or selection was submitted" in kwargs["embed"].description


def test_detached_research_or_data_entry_uses_persistent_fallback():
    for scope in ("research", "data"):
        key = (2, f"rival:{scope}:refresh")
        stale = SimpleNamespace(view=None)
        healthy = SimpleNamespace(view=object())
        found = {}
        views = {11: {key: stale}, None: {key: healthy}}
        def dispatch(*args, _found=found, _views=views, _key=key):
            _found["item"] = _views[11].get(_key) or _views[None][_key]
            return _found["item"]
        store = SimpleNamespace(_views=views, dispatch_view=dispatch)
        client = SimpleNamespace(_connection=SimpleNamespace(_view_store=store))
        install_view_dispatch_logging(client)
        result = store.dispatch_view(*key, SimpleNamespace(
            message=SimpleNamespace(id=11),
        ))
        assert result is healthy
        assert key not in views[11]
