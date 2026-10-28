"""Research dashboards must remain short even when many analytics exist.

Each screen is mobile-sized; all details are reachable by real select controls.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from parlay_bot.discord_app.research_dashboard import (
    _SECTIONS,
    ResearchDashboard,
    _compact_embed,
)


def expanded_report() -> discord.Embed:
    old = discord.Embed(title="RivaL | BTC Research", description="Long old UI")
    old.timestamp = datetime.now(UTC)
    fields = (
        ("🟢 Live collection status", "**8/10** recently collecting • checked 2 minutes ago"),
        ("Six-month history target", "1,085 / 527,040 five-minute bars • 0.206%"),
        ("Historical gap audit", "0/10 audited streams complete"),
        ("Gate six-month recovery", "Recovering history from Gate"),
        ("KuCoin six-month recovery", "Recovering KuCoin"),
        ("Historical recovery progress", "Gate 100, KuCoin 200, Bitget 300 imported"),
        ("Mathematical model", "Background shadow evaluation; no live signals."),
        ("Current market pulse · NOT a trading signal", "8 fresh venues; +0.057% observed 5m median; no trading signal."),
        ("Automatic shadow evaluation", "9 streams, 99 evaluated"),
        ("Automatic learning engine · LIVE", "Last cycle 3 minutes ago"),
        ("Mathematical forecast readiness · LIVE", "8 eligible streams under evaluation"),
        ("Why this exchange is or isn't ready", "hyperliquid needs live history"),
        ("Forward 5-minute evaluation", "Issued 119, settled 114, baseline pending"),
        ("Forward 15-minute evaluation", "Issued 4, settled 0"),
        ("Tonight's research verdict", "NO VERIFIED BETTING EDGE."),
        ("Math 5m · independent UTC windows", "Brier 0.2537 vs baseline 0.2559"),
        ("Math 15m · independent UTC windows", "No settled samples"),
        ("Gemini · independent forward outcomes", "0 Gemini decisions settled"),
        ("How accuracy is audited", "Causal forecasts before outcomes"),
        ("AI evidence", "Uses independent healthy exchanges"),
        ("Gemini model & request limits", "1/24 requests today"),
        ("Analysis status", "On-demand analysis available"),
    )
    for name, value in fields:
        old.add_field(name=name, value=value, inline=False)
    return old


@pytest.mark.parametrize("screen", ["math", "ai", "accuracy"])
def test_no_research_screen_turns_into_an_entire_grocery_list(screen):
    old = expanded_report()
    for section, _ in _SECTIONS[screen]:
        result = _compact_embed(old, screen=screen, section=section)
        assert 1 <= len(result.fields) <= 3
        assert result.timestamp == old.timestamp
        assert "Live market feeds" in [f.name for f in result.fields]
        assert "Choose a section" in result.description
        assert len(str(result.to_dict())) < 1800


def test_overview_shows_useful_verified_summary_without_20_fields():
    result = _compact_embed(expanded_report(), screen="math", section="overview")
    names = [f.name for f in result.fields]
    assert names == [
        "Live market feeds", "Current market pulse · NOT a trading signal",
        "Six-month history target",
    ]
    assert "8/10" in result.fields[0].value
    assert "0.206%" in result.fields[2].value
    accuracy = _compact_embed(expanded_report(), screen="accuracy", section="overview")
    assert "NO VERIFIED BETTING EDGE" in accuracy.fields[1].value
    assert "Issued 119" in accuracy.fields[2].value


class Response:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class Interaction:
    def __init__(self):
        self.user = SimpleNamespace(id=42)
        self.response = Response()
        self.view = None
        self.embed = None

    async def edit_original_response(self, *, embed, view):
        self.embed = embed
        self.view = view


def control(view: ResearchDashboard, key: str):
    return next(item for item in view.children if item.custom_id == key)


@pytest.mark.asyncio
async def test_real_section_selector_and_tab_buttons_are_clickable(monkeypatch):
    view = ResearchDashboard(
        owner_id=42, market=None, ai=None,
        venues=("hyperliquid", "dydx", "gate"),
    )

    async def full():
        return expanded_report()

    monkeypatch.setattr(view, "_render_full", full)
    try:
        assert view.section == "overview"
        select = control(view, "rival:research:section")
        assert select.row == 3
        assert len(select.options) == len(_SECTIONS["math"])
        assert any(option.default and option.value == "overview"
                   for option in select.options)
        view.section = "recovery"
        interaction = Interaction()
        await view.redraw(interaction)
        assert interaction.response.deferred
        assert interaction.view is view
        assert len(interaction.embed.fields) <= 3
        assert "Historical recovery progress" in [f.name for f in interaction.embed.fields]
        assert "Bitget" in interaction.embed.fields[-1].value

        button = control(view, "rival:research:accuracy")
        clicked = Interaction()
        await button.callback(clicked)
        assert clicked.view is view
        assert view.screen == "accuracy" and view.section == "overview"
        assert len(clicked.embed.fields) == 3
        assert not control(view, "rival:research:accuracy").disabled

        view.section = "venue"
        view._rebuild()
        assert control(view, "rival:research:venue").row == 4
        assert len(control(view, "rival:research:section").options) == 8

        view.section = "five"
        outcome = await view.render()
        assert "Forward 5-minute evaluation" in [f.name for f in outcome.fields]
        assert "Forward 15-minute evaluation" not in [f.name for f in outcome.fields]
        view.section = "fifteen"
        outcome = await view.render()
        assert "Forward 15-minute evaluation" in [f.name for f in outcome.fields]
        assert "Forward 5-minute evaluation" not in [f.name for f in outcome.fields]
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_database_error_surfaces_brief_unavailable_state(monkeypatch):
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=(),
    )
    async def full():
        old = discord.Embed(title="RivaL Research", description="unavailable")
        old.add_field(name="Live collection status",
                      value="Current PostgreSQL collection status unavailable.",
                      inline=False)
        return old
    monkeypatch.setattr(dashboard, "_render_full", full)
    try:
        result = await dashboard.render()
        assert len(result.fields) <= 3
        assert "unavailable" in result.fields[0].value
        assert "No results yet" in [x.name for x in result.fields]
    finally:
        dashboard.stop()
