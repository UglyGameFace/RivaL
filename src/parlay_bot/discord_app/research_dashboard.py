"""RivaL's private dual-track research menu. No listicle of ten feeds."""
from __future__ import annotations

import asyncio
import logging

import discord

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence
from parlay_bot.perpetuals.gemini_research import AiResearch, GeminiResearch, ResearchUnavailable
from parlay_bot.perpetuals.history_inventory import history_inventory
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)


class _AssetSelect(discord.ui.Select["ResearchDashboard"]):
    def __init__(self, current: str) -> None:
        super().__init__(
            placeholder="Choose a coin",
            options=[
                discord.SelectOption(label=a, value=a, default=a == current)
                for a in ("BTC", "ETH", "SOL")
            ],
            custom_id="rival:research:asset", row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.asset = self.values[0]
        self.view.opinion = None
        self.view.message = None
        await self.view.redraw(interaction)


class _ScreenButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self, screen: str) -> None:
        super().__init__(
            label="Math & History" if screen == "math" else "Gemini AI",
            emoji="📐" if screen == "math" else "✨",
            style=discord.ButtonStyle.success if screen == "math"
                  else discord.ButtonStyle.secondary,
            custom_id=f"rival:research:{screen}", row=0,
        )
        self.screen = screen

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.screen = self.screen
        self.view.message = None
        await self.view.redraw(interaction)


class _AskButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self, enabled: bool) -> None:
        super().__init__(
            label="Analyze current data", emoji="🧠",
            style=discord.ButtonStyle.primary,
            custom_id="rival:research:analyze", row=2, disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # clear_items() in _rebuild() detaches this button from its View.
        # Hold the owning View before awaiting or rebuilding; self.view becomes
        # None after detachment and must not be consulted again.
        dashboard = self.view
        assert dashboard is not None
        await interaction.response.defer()
        dashboard.screen = "ai"
        dashboard.message = None
        dashboard.opinion = None
        try:
            if dashboard.ai is None:
                raise ResearchUnavailable("Gemini research is unavailable.")
            dashboard.opinion = await dashboard.ai.ask(
                asset=dashboard.asset, venues=dashboard.venues,
            )
        except (ResearchUnavailable, InsufficientEvidence) as exc:
            dashboard.message = str(exc)
        except Exception:  # noqa: BLE001 - don't leak API keys/database DSNs
            _LOG.exception("RivaL research analysis failed")
            dashboard.message = "Analysis unavailable. No prediction produced."
        dashboard._rebuild()
        await interaction.edit_original_response(
            embed=await dashboard.render(), view=dashboard,
        )


class ResearchDashboard(discord.ui.View):
    def __init__(
        self, *, owner_id: int, market: PerpetualMarketStore | None,
        ai: GeminiResearch | None, venues: tuple[str, ...],
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.market = market
        self.ai = ai
        self.venues = venues
        self.asset = "BTC"
        self.screen = "math"
        self.opinion: AiResearch | None = None
        self.message: str | None = None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Open your own private research panel with /rival-research.",
                ephemeral=True,
            )
            return False
        return True

    def _rebuild(self) -> None:
        self.clear_items()
        for name in ("math", "ai"):
            item = _ScreenButton(name)
            item.disabled = self.screen == name
            item.style = (discord.ButtonStyle.success if self.screen == name
                          else discord.ButtonStyle.secondary)
            self.add_item(item)
        self.add_item(_AssetSelect(self.asset))
        if self.screen == "ai":
            self.add_item(_AskButton(
                self.market is not None and self.ai is not None and self.ai.available
            ))

    async def render(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"RivaL  |  {self.asset} Research",
            description=(
                "**Two independent tracks** · No validated betting edge. "
                "Market collection continues regardless of Gemini."
            ),
            color=discord.Color.green(),
        )
        if self.screen == "math":
            if self.market is None:
                embed.add_field(
                    name="Mathematical analysis",
                    value="PostgreSQL collector unavailable.", inline=False,
                )
            else:
                try:
                    status = await asyncio.to_thread(
                        history_inventory, self.market,
                        asset=self.asset, venues=self.venues,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("History audit unavailable")
                    embed.add_field(
                        name="History", value="Coverage check unavailable.",
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="Six-month history target",
                        value=(
                            f"**{status['saved']:,} / {status['expected']:,}** "
                            "five-minute observations across configured streams.\n"
                            f"**{status['coverage_pct_ceiling']:.3f}%** "
                            "maximum possible row-count coverage. "
                            "Gaps and real-time availability need separate audits."
                        ), inline=False,
                    )
                    embed.add_field(
                        name="Mathematical model",
                        value=(
                            "Point-in-time features and shadow evaluation exist. "
                            "**Scheduled training, calibration and verified "
                            "production signals are not active.**"
                        ), inline=False,
                    )
            embed.set_footer(
                text="Historical backfills do not count as live forward tests."
            )
            return embed

        embed.add_field(
            name="AI evidence",
            value=(
                "Gemini analyzes **all currently healthy distinct exchanges**, "
                "including available 15m, 1h, 4h and 24h return/volatility "
                "features. Unavailable sources are excluded and disclosed."
            ), inline=False,
        )
        if self.opinion is not None and self.opinion.asset == self.asset:
            answer = self.opinion
            embed.add_field(
                name="Gemini assessment · UNCALIBRATED",
                value=(
                    f"Direction: **{answer.direction.title()}** · "
                    f"{answer.confidence} self-reported confidence\n"
                    f"Feeds: {answer.source_count} · "
                    f"{'Database cache' if answer.from_cache else 'New response'}\n"
                    f"Reason: {discord.utils.escape_mentions(answer.thesis)}\n"
                    f"Risk: {discord.utils.escape_mentions(answer.risk)}"
                )[:1024], inline=False,
            )
        else:
            embed.add_field(
                name="Analysis status",
                value=(
                    self.message or (
                        "Gemini is ready for on-demand, cached analysis."
                        if self.ai is not None and self.ai.available else
                        "No Gemini API key is visible to this RivaL process."
                    )
                ), inline=False,
            )
        embed.set_footer(
            text="New candles invalidate cached evidence • Daily DB quota • Not financial advice"
        )
        return embed

    async def redraw(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self._rebuild()
        await interaction.edit_original_response(embed=await self.render(), view=self)
