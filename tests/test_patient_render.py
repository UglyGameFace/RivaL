"""A slow screen finishes itself instead of asking for a second press."""
from __future__ import annotations

import asyncio

import discord
import pytest

from parlay_bot.discord_app.patient_render import patient


def _embed(title):
    return discord.Embed(title=title)


@pytest.mark.asyncio
async def test_a_fast_render_is_returned_without_a_note():
    notes = []

    async def render():
        return _embed("ready")

    async def slow():
        notes.append(1)

    assert (await patient(render, on_slow=slow, first=.2, total=1)).title == "ready"
    assert notes == []


@pytest.mark.asyncio
async def test_a_slow_render_shows_a_note_then_its_own_result():
    """The SAME render finishes: it is not restarted or abandoned."""
    notes, starts = [], []

    async def render():
        starts.append(1)
        await asyncio.sleep(.3)
        return _embed("finished")

    async def slow():
        notes.append("still loading")

    result = await patient(render, on_slow=slow, first=.05, total=2)
    assert result.title == "finished" and notes == ["still loading"] and starts == [1]


@pytest.mark.asyncio
async def test_only_a_render_slower_than_the_total_times_out_and_is_stopped():
    stopped = []

    async def render():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            stopped.append(1)
            raise

    with pytest.raises(TimeoutError):
        await patient(render, first=.05, total=.15)
    await asyncio.sleep(0)
    assert stopped == [1]


@pytest.mark.asyncio
async def test_a_newer_press_stops_the_older_render():
    stopped = []

    async def render():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            stopped.append(1)
            raise

    waiter = asyncio.ensure_future(patient(render, first=1, total=2))
    await asyncio.sleep(.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    assert stopped == [1]


class _Interaction:
    def __init__(self):
        self.shown = []

    async def edit_original_response(self, *, embed, view, content=None):
        self.shown.append(embed.title)


@pytest.mark.asyncio
async def test_detailed_research_fills_itself_in_after_a_slow_read(monkeypatch):
    from parlay_bot.discord_app import patient_render
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard

    monkeypatch.setattr(patient_render, "FIRST_WAIT", .05)
    view = ResearchDashboard(owner_id=1, market=None, ai=None, venues=())

    async def slow_render():
        await asyncio.sleep(.2)
        return discord.Embed(title="Results")

    fresh = view._display_generation()
    fresh.render = slow_render
    interaction = _Interaction()
    state = view._navigation_state
    state["revision"] = 7
    # patient() reads its defaults at definition; pass the short wait through.
    monkeypatch.setattr(
        "parlay_bot.discord_app.research_dashboard.patient",
        lambda render, on_slow=None: patient_render.patient(
            render, on_slow=on_slow, first=.05, total=2))
    await view._finish_redraw(interaction, fresh, 7)
    assert interaction.shown == ["⏳ Still loading…", "Results"]


@pytest.mark.asyncio
async def test_a_newer_press_keeps_an_old_slow_screen_off_the_message(monkeypatch):
    from parlay_bot.discord_app import patient_render
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard

    view = ResearchDashboard(owner_id=1, market=None, ai=None, venues=())

    async def slow_render():
        await asyncio.sleep(.2)
        return discord.Embed(title="Old screen")

    fresh = view._display_generation()
    fresh.render = slow_render
    interaction = _Interaction()
    view._navigation_state["revision"] = 8     # the user pressed again meanwhile
    monkeypatch.setattr(
        "parlay_bot.discord_app.research_dashboard.patient",
        lambda render, on_slow=None: patient_render.patient(
            render, on_slow=on_slow, first=.05, total=2))
    await view._finish_redraw(interaction, fresh, 7)
    assert interaction.shown == []
