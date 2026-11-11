"""Audit Step 3: 14 Results sections became 5 + the raw technical page.

Nothing is deleted: every old section survives as a detail topic inside
its new home, one tap behind a detail button. Old keys resolve to their
consolidated card; a stale menu or deep link never lands on a dead
section. Numbers below are the worst case from Steps 1–2 (5 graded at
100% hidden, 30 graded at 60%, 8 of 10 feeds, 25 of 65 live bars), never
placeholder mockups.
"""
from __future__ import annotations

import pytest

from parlay_bot.discord_app.research_dashboard import (
    _SECTIONS,
    ACCURACY_DETAIL_LABELS,
    ACCURACY_SECTIONS,
    LEGACY_SECTION_MAP,
    ResearchDashboard,
    _resolve_section,
)
from tests.test_audit_step1_card_quick_fixes import (
    _accuracy_dashboard,
    _analytics_details,
)
from tests.test_research_compact_navigation import expanded_report


def _readiness_dict() -> dict:
    """Worst case: 8 of 10 exchanges eligible, streak 25 of 65 bars."""
    return {
        "eligible": 8, "configured": 10, "best_live_streak": 25,
        "min_live_bars": 65, "best_causal_5m": 30, "best_evaluated_5m": 12,
        "min_evaluations": 50, "stale_or_blocked": 2, "waiting_history": 0,
        "streams": [{
            "venue": "hyperliquid", "status": "needs_live_history",
            "recent_live_streak": 25, "observations": 130,
            "causal_5m": 30, "causal_15m": 12,
            "evaluated_5m": 12, "evaluated_15m": 4,
        }],
    }


class _Response:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class _Interaction:
    def __init__(self):
        from types import SimpleNamespace

        self.user = SimpleNamespace(id=42)
        self.response = _Response()
        self.view = None
        self.embed = None

    async def edit_original_response(self, *, embed, view):
        self.embed = embed
        self.view = view


def _control(view: ResearchDashboard, key: str):
    return next(item for item in view.children if item.custom_id == key)


def test_accuracy_menu_lists_five_sections_plus_technical():
    # Nov 11 cleanup: Results opens on the models; "At a glance" duplicated
    # Bot Performance and its key now opens Models on trial.
    order = ["better", "money", "accuracy", "running", "technical"]
    assert [key for key, _ in _SECTIONS["accuracy"]] == order
    assert dict(_SECTIONS["accuracy"]) == {
        "better": "Models on trial",
        "money": "Would it make money?",
        "accuracy": "Accuracy details",
        "running": "Running details",
        "technical": "Technical evidence",
    }
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=("hyperliquid",),
    )
    try:
        dashboard.screen = "accuracy"
        dashboard._rebuild()
        select = _control(dashboard, "rival:research:section")
        assert [o.value for o in select.options] == order
        assert len(select.options) == 5
    finally:
        dashboard.stop()


@pytest.mark.parametrize("old,new", [
    ("engine", "running"), ("readiness", "running"), ("venue", "running"),
    ("five", "accuracy"), ("fifteen", "accuracy"),
    ("calibration", "accuracy"), ("ledger", "accuracy"),
    ("progress", "better"), ("lineage", "better"), ("tracker", "better"),
    ("costs", "money"), ("method", "money"),
    ("overview", "better"), ("technical", "technical"),
])
def test_every_old_section_key_resolves_to_its_new_card(old, new):
    assert _resolve_section("accuracy", old) == new
    assert LEGACY_SECTION_MAP[old] == new
    assert old in ACCURACY_SECTIONS[new] or old == "overview"
    if old not in ("overview", "technical"):
        assert ACCURACY_DETAIL_LABELS[old]
    # Other screens are untouched by the mapping.
    assert _resolve_section("math", old) == old


@pytest.mark.asyncio
async def test_stale_section_assignment_lands_on_the_consolidated_card(monkeypatch):
    dashboard = _accuracy_dashboard(monkeypatch, section="engine")
    try:
        # _rebuild already resolves the stale key on arrival.
        assert dashboard.section == "running"
        dashboard.section = "engine"  # stale again, as a deep link would
        embed = await dashboard.render()
        assert dashboard.section == "running"
        names = [f.name for f in embed.fields]
        assert "Automatic learning engine · LIVE" in names
        assert "Forward 5-minute evaluation" not in names
        dashboard.section = "five"
        embed = await dashboard.render()
        assert dashboard.section == "accuracy"
        names = [f.name for f in embed.fields]
        assert "Forward 5-minute evaluation" in names
        assert "Forward 15-minute evaluation" in names
    finally:
        dashboard.stop()


@pytest.mark.asyncio
async def test_consolidated_cards_show_headline_answers_not_other_topics(monkeypatch):
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=("hyperliquid",),
    )
    dashboard.screen = "accuracy"
    dashboard._rebuild()

    async def full():
        return expanded_report()

    monkeypatch.setattr(dashboard, "_render_full", full)
    try:
        dashboard.section = "running"
        names = [f.name for f in (await dashboard.render()).fields]
        assert "Automatic learning engine · LIVE" in names
        assert "Mathematical forecast readiness · LIVE" in names
        assert "Forward 5-minute evaluation" not in names
        assert "How accuracy is audited" not in names

        dashboard.section = "accuracy"
        names = [f.name for f in (await dashboard.render()).fields]
        assert "Forward 5-minute evaluation" in names
        assert "Forward 15-minute evaluation" in names
        assert "Mathematical forecast readiness · LIVE" not in names
        assert "How accuracy is audited" not in names

        dashboard.section = "money"
        names = [f.name for f in (await dashboard.render()).fields]
        assert "How accuracy is audited" in names
        assert "Forward 5-minute evaluation" not in names
        assert "Automatic learning engine · LIVE" not in names
        # Every consolidated compact stays phone-sized.
        assert len(names) <= 3
    finally:
        dashboard.stop()


def test_each_consolidated_card_has_one_detail_button_per_topic():
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=("hyperliquid",),
    )
    try:
        dashboard.screen = "accuracy"
        menu = dict(_SECTIONS["accuracy"])
        for section, topics in ACCURACY_SECTIONS.items():
            if section not in menu:
                continue   # "overview" opens Models on trial now
            dashboard.section = section
            dashboard.detail = None
            dashboard._rebuild()
            buttons = [
                item for item in dashboard.children
                if item.custom_id.startswith("rival:research:detail:")
            ]
            if len(topics) == 1:
                assert buttons == []
            else:
                assert [b.topic for b in buttons] == list(topics)
                assert all(b.row == 2 for b in buttons)
                assert [b.label for b in buttons] == [
                    ACCURACY_DETAIL_LABELS[t] for t in topics
                ]
    finally:
        dashboard.stop()


@pytest.mark.asyncio
async def test_detail_button_opens_its_topic_and_back_returns(monkeypatch):
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=("hyperliquid",),
    )
    dashboard.screen = "accuracy"
    dashboard.section = "accuracy"
    dashboard._rebuild()

    async def full():
        return expanded_report()

    monkeypatch.setattr(dashboard, "_render_full", full)
    try:
        button = _control(dashboard, "rival:research:detail:five")
        interaction = _Interaction()
        await button.callback(interaction)
        assert dashboard.detail == "five"
        names = [f.name for f in interaction.embed.fields]
        assert "Forward 5-minute evaluation" in names
        assert "Forward 15-minute evaluation" not in names
        assert "**5-minute predictions**" in interaction.embed.description

        back = _control(dashboard, "rival:research:detail:back")
        assert back.label == "← Back to Accuracy details"
        assert back.row == 2
        interaction = _Interaction()
        await back.callback(interaction)
        assert dashboard.detail is None
        names = [f.name for f in interaction.embed.fields]
        assert "Forward 5-minute evaluation" in names
        assert "Forward 15-minute evaluation" in names
    finally:
        dashboard.stop()


@pytest.mark.asyncio
async def test_section_select_clears_the_open_detail(monkeypatch):
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=("hyperliquid",),
    )
    dashboard.screen = "accuracy"
    dashboard.section = "better"
    dashboard.detail = "lineage"
    dashboard._rebuild()
    try:
        select = _control(dashboard, "rival:research:section")
        select._values = ["money"]
        await select.callback(_Interaction())
        assert dashboard.detail is None
        assert dashboard.section == "money"
    finally:
        dashboard.stop()


def test_stale_detail_topic_from_another_section_is_ignored():
    dashboard = ResearchDashboard(
        owner_id=42, market=None, ai=None, venues=("hyperliquid",),
    )
    try:
        dashboard.screen = "accuracy"
        dashboard.section = "running"
        dashboard.detail = "five"  # belongs to "How accurate is it?"
        assert dashboard._render_topics() == ("engine", "readiness", "venue")
        dashboard.detail = "engine"
        assert dashboard._render_topics() == ("engine",)
    finally:
        dashboard.stop()


# --- No absorbed topic's content is dropped ----------------------------------

_TOPIC_FIELDS = {
    "engine": ("Automatic learning engine · LIVE",),
    "readiness": ("Mathematical forecast readiness · LIVE",),
    "venue": ("Why this exchange is or isn't ready",),
    "five": ("Forward 5-minute evaluation",),
    "fifteen": ("Forward 15-minute evaluation",),
    "calibration": ("Forward 5m calibration", "Forward 15m calibration"),
    "ledger": ("Prediction ledger · prospective",),
    "progress": ("Future model proof",),
    "lineage": ("How is RivaL learning?",),
    "tracker": ("Prediction tracking",),
    "costs": ("Execution cost verification",),
    "method": ("How accuracy is audited",),
    "technical": ("Model training actually used",
                  # Step 2 renames "Boosted ..." to the one display name.
                  "Boosted history model production proof"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", list(_TOPIC_FIELDS))
async def test_every_absorbed_topic_is_reachable_with_its_content(
        monkeypatch, topic):
    """Each old section's fields render behind its detail button."""
    from parlay_bot.discord_app import research_dashboard as rd_module

    home = next(
        new for new, olds in ACCURACY_SECTIONS.items() if topic in olds)
    monkeypatch.setattr(rd_module, "analytics_snapshot",
                        lambda *_, **__: _analytics_details())
    monkeypatch.setattr(rd_module, "forecast_readiness",
                        lambda *_, **__: _readiness_dict())
    dashboard = _accuracy_dashboard(monkeypatch, section=home)
    dashboard.detail = topic
    try:
        embed = await dashboard._render_full()
    finally:
        dashboard.stop()
    names = [f.name for f in embed.fields]
    for expected in _TOPIC_FIELDS[topic]:
        assert expected in names, f"{topic}: {expected} missing from {names}"


@pytest.mark.asyncio
async def test_consolidated_full_renders_carry_the_union(monkeypatch):
    """The consolidated card builds every absorbed topic's fields."""
    from parlay_bot.discord_app import research_dashboard as rd_module

    monkeypatch.setattr(rd_module, "analytics_snapshot",
                        lambda *_, **__: _analytics_details())
    monkeypatch.setattr(rd_module, "forecast_readiness",
                        lambda *_, **__: _readiness_dict())
    dashboard = _accuracy_dashboard(monkeypatch, section="running")
    try:
        names = [f.name for f in (await dashboard._render_full()).fields]
        assert "Automatic learning engine · LIVE" in names
        assert "Mathematical forecast readiness · LIVE" in names
        assert "Why this exchange is or isn't ready" in names
        assert "Forward 5-minute evaluation" not in names

        dashboard.section = "accuracy"
        names = [f.name for f in (await dashboard._render_full()).fields]
        assert "Forward 5-minute evaluation" in names
        assert "Forward 15-minute evaluation" in names
        assert "Forward 5m calibration" in names
        assert "Prediction ledger · prospective" in names
        assert "How accuracy is audited" not in names

        dashboard.section = "better"
        names = [f.name for f in (await dashboard._render_full()).fields]
        assert "Future model proof" in names
        assert "How is RivaL learning?" in names
        assert "Prediction tracking" in names
        assert "Forward 5-minute evaluation" not in names

        dashboard.section = "money"
        names = [f.name for f in (await dashboard._render_full()).fields]
        assert "Execution cost verification" in names
        assert "How accuracy is audited" in names
        assert "Forward 5-minute evaluation" not in names
    finally:
        dashboard.stop()
