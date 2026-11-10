"""Restart-safe recovery for the private Research and Data dashboards.

Only the component ID is reused. A stale tap never replays AI spending,
personal follow actions, or selections from an old view.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import discord

from parlay_bot.discord_app.research_dashboard import ACCURACY_SECTIONS

_LOG = logging.getLogger(__name__)

RESEARCH_BUTTONS = (
    "opportunities", "math", "ai", "accuracy", "refresh",
    "follow-target", "analyze", "track-venue", "track-platform",
    # Audit Step 3: the consolidated cards' back button.
    "detail:back",
)
# Audit Step 3: one recovery button per absorbed detail topic, so a
# stale tap on a detail button opens a fresh panel like every other
# live control.
RESEARCH_DETAIL_BUTTONS = tuple(
    f"detail:{topic}"
    for section in ("running", "accuracy", "better", "money")
    for topic in ACCURACY_SECTIONS[section]
)
RESEARCH_SELECTS = ("asset", "section", "venue")
# A view has five rows and each select fills one, so the tracker's select
# lives in a second research fallback view (dispatch matches by custom_id
# across every registered persistent view).
RESEARCH_EXTRA_SELECTS = ("track-start",)
DATA_BUTTONS = (
    "overview", "exchanges", "markets", "coverage",
    "refresh", "previous", "next",
)
DATA_SELECTS = ("choose:exchange", "choose:coverage", "choose:market")


class _RecoverButton(discord.ui.Button["StaleDashboardView"]):
    def __init__(self, scope: str, action: str, index: int) -> None:
        super().__init__(
            custom_id=f"rival:{scope}:{action}",
            label="Open fresh panel", style=discord.ButtonStyle.secondary,
            row=index // 5,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.recover(interaction, self.action)


class _RecoverSelect(discord.ui.Select["StaleDashboardView"]):
    def __init__(self, scope: str, action: str, row: int) -> None:
        # Discord dispatch matches the exact component ID, not the old
        # options. Selections from timed-out menus are intentionally ignored.
        super().__init__(
            custom_id=f"rival:{scope}:{action}",
            placeholder="Open a new panel to choose",
            options=[discord.SelectOption(label="Open fresh panel", value="reopen")],
            row=row,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.recover(interaction, self.action)


class StaleDashboardView(discord.ui.View):
    """Persistent dispatch-only fallback, no user or message state saved."""

    def __init__(self, scope: str, factory: Callable[[int], discord.ui.View], *,
                 buttons: tuple[str, ...] | None = None,
                 selects: tuple[str, ...] | None = None) -> None:
        if scope not in ("research", "data"):
            raise ValueError("unsupported stale dashboard")
        super().__init__(timeout=None)
        self.scope = scope
        self.factory = factory
        if buttons is None:
            buttons = RESEARCH_BUTTONS if scope == "research" else DATA_BUTTONS
        if selects is None:
            selects = RESEARCH_SELECTS if scope == "research" else DATA_SELECTS
        for index, action in enumerate(buttons):
            self.add_item(_RecoverButton(scope, action, index))
        first_select_row = (len(buttons) + 4) // 5
        for index, action in enumerate(selects):
            self.add_item(_RecoverSelect(scope, action, first_select_row + index))

    async def recover(self, interaction: discord.Interaction, action: str) -> None:
        # Always acknowledge immediately; never execute the expired action,
        # especially Analyze (LLM quota) or Follow (personal notifications).
        await interaction.response.defer()
        dashboard = self.factory(interaction.user.id)
        try:
            embed = await dashboard.render()
            embed.description = (
                "This older panel has been refreshed. No previous action or "
                "selection was submitted. Use the new controls below.\n\n"
                + (embed.description or "")
            )
            await interaction.edit_original_response(embed=embed, view=dashboard)
            _LOG.info(
                "RivaL stale %s dashboard recovered action=%s user_id=%s",
                self.scope, action, interaction.user.id,
            )
        except Exception:  # noqa: BLE001 - private recovery is best effort
            _LOG.exception("RivaL stale %s dashboard recovery failed", self.scope)
            await interaction.edit_original_response(
                content=(
                    "That old panel could not be refreshed. Open "
                    + ("/rival-tools research" if self.scope == "research"
                       else "/rival-tools data")
                    + " for a new private panel."
                ),
                embed=None,
                view=None,
            )


def build_stale_dashboard_views(
    research_factory: Callable[[int], discord.ui.View],
    data_factory: Callable[[int], discord.ui.View],
) -> tuple[StaleDashboardView, ...]:
    return (
        StaleDashboardView("research", research_factory),
        StaleDashboardView("research", research_factory, buttons=(),
                           selects=RESEARCH_EXTRA_SELECTS),
        StaleDashboardView("research", research_factory,
                           buttons=RESEARCH_DETAIL_BUTTONS, selects=()),
        StaleDashboardView("data", data_factory),
    )
