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
    _history_headline,
    _plain_boosted_proof,
    _plain_training_line,
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
        ("Spike watch analytics", "Observed 3 spikes"),
        ("Recent spike events", "Verified anomalies"),
        ("Automatic shadow evaluation", "9 streams, 99 evaluated"),
        ("Automatic learning engine · LIVE", "Last cycle 3 minutes ago"),
        ("Mathematical forecast readiness · LIVE", "8 eligible streams under evaluation"),
        ("Why this exchange is or isn't ready", "hyperliquid needs live history"),
        ("Forward 5-minute evaluation", "Issued 119, settled 114, baseline pending"),
        ("Forward 15-minute evaluation", "Issued 4, settled 0"),
        ("Research verdict", "NO VERIFIED BETTING EDGE."),
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
    spikes = _compact_embed(expanded_report(), screen="math", section="spikes")
    assert [f.name for f in spikes.fields] == [
        "Live market feeds", "Spike watch analytics", "Recent spike events",
    ]
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
    view.screen = "math"
    view._rebuild()
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
        assert len(control(view, "rival:research:section").options) == len(_SECTIONS["accuracy"])

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
    dashboard.screen = "math"
    dashboard._rebuild()
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


def test_research_menu_consolidates_legacy_ai_and_keeps_all_four_assets():
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None,
        venues=("hyperliquid", "okx", "gate"),
    )
    try:
        assert {
            item.custom_id for item in dashboard.children
            if isinstance(item, discord.ui.Button)
            and item.row == 0
        } == {
            "rival:research:opportunities",
            "rival:research:math",
            "rival:research:accuracy",
        }
        assert not any(
            "Gemini" in getattr(item, "label", "")
            for item in dashboard.children
        )
        asset = control(dashboard, "rival:research:asset")
        assert {option.value for option in asset.options} == {
            "BTC", "ETH", "SOL", "XRP",
        }
        dashboard.screen = "math"
        dashboard.section = "ai"
        dashboard._rebuild()
        assert control(dashboard, "rival:research:analyze").disabled
        assert "AI Review" in [label for _, label in _SECTIONS["math"]]
        assert not any(label == "Gemini results" for _, label in _SECTIONS["accuracy"])
        assert control(dashboard, "rival:research:section").row == 3
    finally:
        dashboard.stop()


@pytest.mark.asyncio
async def test_ai_review_is_reachable_inside_math_without_old_ai_tab(monkeypatch):
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=(),
    )
    dashboard.screen, dashboard.section = "math", "ai"
    dashboard._rebuild()

    async def detail():
        embed = discord.Embed(title="RivaL Intelligence")
        embed.add_field(name="Live collection status",
                        value="No feed evidence", inline=False)
        embed.add_field(name="Analysis status",
                        value="Private AI reviewer not configured", inline=False)
        return embed

    monkeypatch.setattr(dashboard, "_render_full", detail)
    try:
        shown = await dashboard.render()
        assert "AI Review" in shown.description
        assert "Private AI reviewer not configured" in shown.fields[-1].value
        assert "Gemini" not in str(shown.to_dict())
        assert len(shown.fields) <= 3
    finally:
        dashboard.stop()


def test_historical_learning_is_explained_without_claiming_improvement():
    proof = {
        "forecasts": 100,
        "latest_lineage_verified": True,
        "latest_historical_seed_sha256": None,
        "latest_online_training_updates": 1434,
        "historically_seeded": 0,
        "lineage_proven": 100,
    }
    explanation = _plain_training_line(proof, 5)
    assert "live data only" in explanation
    assert "1,434 live updates so far" in explanation
    assert "history used in 0 of the last 100 predictions" in explanation
    assert "Is it learning from history? No." in _history_headline([proof])

    proof["latest_historical_seed_sha256"] = "verified-hash"
    assert "verified historical model" in _plain_training_line(proof, 5)
    proof["latest_lineage_verified"] = False
    proof["lineage_unknown"] = 90
    assert "Cannot confirm" in _plain_training_line(proof, 5)
    proof["forecasts"] = 0
    assert "No recorded predictions" in _plain_training_line(proof, 5)


def test_advanced_models_show_screening_blocker_not_paper_profit():
    proof = {
        "portable_imported": 54,
        "portable_screened": 0,
        "screening_blockers": [
            {"count": 54, "reason": "validation_brier_gain_below_0.005"},
        ],
        "issued": 0,
        "settled": 0,
        "quote_exits": 0,
        "fresh_paper_promotions": [],
    }
    explanation = _plain_boosted_proof(proof)
    assert "**Advanced models built from history so far:** 54" in explanation
    assert "**Passed screening:** 0" in explanation
    assert "**Advanced-model forecasts (30d):** 0 logged" in explanation
    assert "Main model: see Results for separate forward records." in explanation
    assert "did not improve enough over basic forecasts in validation" in explanation
    assert "No model qualifies for simulated testing yet" in explanation
    assert "Real profit" in explanation
    assert len(explanation) < 1024

    # Boosted and incumbent forecasts have different lineage and denominators.
    incumbent = {"last_7d": {
        5: {"issued": 125, "settled": 100},
        15: {"issued": 81, "settled": 65},
        60: {"issued": 30, "settled": 18},
    }}
    with_baseline = _plain_boosted_proof(proof, incumbent=incumbent)
    assert "**Main model (all timeframes, 7d):** 236 predictions, 183 graded." in with_baseline
    assert "**Advanced-model forecasts (30d):** 0 logged" in with_baseline
    assert len(with_baseline) < 1024


def test_research_accuracy_offers_plain_and_technical_sections():
    labels = dict(_SECTIONS["accuracy"])
    assert labels["lineage"] == "Is it learning from history?"
    assert labels["technical"] == "Technical evidence"
    assert len(_SECTIONS["accuracy"]) <= 25
