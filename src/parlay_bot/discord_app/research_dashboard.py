"""RivaL's private dual-track research menu. No listicle of ten feeds."""
from __future__ import annotations

import asyncio
import logging

import discord

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence
from parlay_bot.perpetuals.gemini_research import AiResearch, GeminiResearch, ResearchUnavailable
from parlay_bot.perpetuals.history_inventory import history_inventory
from parlay_bot.perpetuals.market_pulse import calculate_market_pulse
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.shadow_monitor import MIN_EVALUATED, ShadowLearningMonitor

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


class _ModelListButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self, *, enabled: bool) -> None:
        super().__init__(
            label="Check models", emoji="🔍",
            style=discord.ButtonStyle.secondary,
            custom_id="rival:research:models", row=2,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        await interaction.response.defer()
        try:
            if dashboard.ai is None:
                raise ResearchUnavailable("Gemini research is not configured.")
            names = await dashboard.ai.list_advertised_models()
            configured = dashboard.ai.model
            listed = configured in names
            other = [name for name in names if name != configured]
            # Short, mobile-friendly, relevant alternatives only.
            options = ", ".join(other[:5]) or "None listed"
            dashboard.message = (
                f"**Selected for analysis:** `{configured}`\n"
                + (
                    "✅ **Listed for text generation** with this API key.\n"
                    if listed else
                    "⚠️ **Not advertised for text generation** in the "
                    "complete model catalog returned to this API key.\n"
                )
                + f"**Other text models (newest first):** {options[:430]}\n"
                + (
                    f"+{len(other) - 5} more text models available.\n"
                    if len(other) > 5 else ""
                )
                + "This is a model catalog, not a quality ranking or a "
                "successful API generation test."
            )
        except ResearchUnavailable as exc:
            dashboard.message = str(exc)
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL Gemini model inspection failed")
            dashboard.message = "Model lookup failed. No API key disclosed."
        dashboard._rebuild()
        await interaction.edit_original_response(
            embed=await dashboard.render(), view=dashboard,
        )


class ResearchDashboard(discord.ui.View):
    def __init__(
        self, *, owner_id: int, market: PerpetualMarketStore | None,
        ai: GeminiResearch | None, venues: tuple[str, ...],
        shadow_enabled: bool = False,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.market = market
        self.ai = ai
        self.venues = venues
        self.shadow_enabled = shadow_enabled
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
            enabled = self.market is not None and self.ai is not None and self.ai.available
            self.add_item(_AskButton(enabled))
            self.add_item(_ModelListButton(enabled=enabled))

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
                            "Online weights are evaluated in the background on "
                            "settled historical observations. **No model is "
                            "approved for live trading or automated signals.**"
                        ) if self.shadow_enabled else (
                            "Point-in-time features and offline shadow evaluation "
                            "exist; scheduled evaluation is disabled."
                        ), inline=False,
                    )
                # This describes *observed* cross-venue movement; no
                # target/stop-loss/probability is invented from raw momentum.
                try:
                    pulse = await asyncio.to_thread(
                        calculate_market_pulse, self.market,
                        asset=self.asset, venues=self.venues,
                    )
                except InsufficientEvidence:
                    embed.add_field(
                        name="Current market pulse",
                        value=(
                            "NO SIGNAL · Not enough fresh, contiguous "
                            "5-minute candles from at least three exchanges."
                        ), inline=False,
                    )
                except Exception:  # noqa: BLE001 - no credentials in Discord
                    _LOG.exception("RivaL market pulse calculation failed")
                    embed.add_field(
                        name="Current market pulse",
                        value="Current read-only market observations unavailable.",
                        inline=False,
                    )
                else:
                    quarter = (
                        f"{pulse.median_15m_pct:+.3f}%"
                        if pulse.median_15m_pct is not None else "unavailable"
                    )
                    hour = (
                        f"{pulse.median_1h_pct:+.3f}%"
                        if pulse.median_1h_pct is not None else "insufficient history"
                    )
                    embed.add_field(
                        name="Current market pulse · NOT a trading signal",
                        value=(
                            f"**{pulse.sources}/{pulse.configured}** fresh venues "
                            f"· most recent shared close "
                            f"{discord.utils.format_dt(pulse.observed_at, style='R')}\n"
                            f"Previous 5m median move: "
                            f"**{pulse.median_5m_pct:+.3f}%**\n"
                            f"15m context: **{quarter}** · 1h: **{hour}**\n"
                            f"Past 5m sources: **{pulse.up} up / "
                            f"{pulse.down} down / {pulse.flat} flat**\n"
                            f"Exchange range: "
                            f"{pulse.range_5m_pct[0]:+.3f}% to "
                            f"{pulse.range_5m_pct[1]:+.3f}%\n"
                            "**Next 5m forecast: NOT VALIDATED.** "
                            "Suggested take-profit/stop: not approved. "
                            "Past momentum is not the next candle."
                        )[:1024],
                        inline=False,
                    )
                if self.shadow_enabled:
                    try:
                        snapshots = await asyncio.to_thread(
                            ShadowLearningMonitor(self.market.store).latest,
                            asset=self.asset,
                        )
                    except Exception:  # noqa: BLE001 - view must never crash
                        _LOG.exception("Stored shadow evaluation read failed")
                        embed.add_field(
                            name="Automatic learning",
                            value="Learning history unavailable; collection can continue.",
                            inline=False,
                        )
                    else:
                        ready = [
                            row for row in snapshots
                            if row["evaluated"] >= MIN_EVALUATED
                            and row["brier"] is not None
                        ]
                        if ready:
                            total = sum(int(x["evaluated"]) for x in ready)
                            model_score = sum(
                                float(x["brier"]) * int(x["evaluated"])
                                for x in ready
                            ) / total
                            baseline_score = sum(
                                float(x["baseline_brier"]) * int(x["evaluated"])
                                for x in ready
                            ) / total
                            embed.add_field(
                                name="Automatic shadow evaluation",
                                value=(
                                    f"**{len(ready)}** qualifying venue/horizon streams "
                                    f"· **{total:,}** settled evaluations (may overlap).\n"
                                    f"Mean Brier: **{model_score:.4f}** vs "
                                    f"baseline **{baseline_score:.4f}**.\n"
                                    "Research only: not a calibrated or deployed signal."
                                ),
                                inline=False,
                            )
                        else:
                            embed.add_field(
                                name="Automatic shadow evaluation",
                                value=(
                                    f"**{len(snapshots)}** tracked venue/horizon streams. "
                                    f"Waiting for at least {MIN_EVALUATED} causal "
                                    "settled evaluations per stream. Backfilled "
                                    "candles cannot qualify as live predictions."
                                ),
                                inline=False,
                            )
            embed.set_footer(
                text="Historical backfills do not count as live forward tests."
            )
            return embed

        embed.add_field(
            name="AI evidence",
            value=(
                "Gemini analyzes **all currently healthy distinct exchanges**, "
                "for a **5-minute forecast**, with available 5m, 15m, "
                "1h, 4h and 24h returns and volatility "
                "features. Unavailable sources are excluded and disclosed."
            ), inline=False,
        )
        if self.ai is not None and self.ai.available:
            try:
                quota = await self.ai.request_status()
                reasons = {
                    "provider_error_pause": "A previous Google API error caused a pause",
                    "minimum_spacing": "RivaL's minimum interval between AI requests",
                    "daily_budget_exhausted": "RivaL's daily safety limit is reached",
                }
                model = discord.utils.escape_markdown(quota["model"])
                if quota["ready"]:
                    state = "**Available for a new request**"
                else:
                    state = " · ".join(
                        reasons.get(reason, "Temporary pause")
                        for reason in quota["reasons"]
                    )
                    retry = quota["retry_at"]
                    if retry is not None:
                        state += (
                            "\nNext permitted request: "
                            + discord.utils.format_dt(retry, style="f")
                            + " ("
                            + discord.utils.format_dt(retry, style="R")
                            + ")"
                        )
                embed.add_field(
                    name="Gemini model & request limits",
                    value=(
                        f"Model: **{model}**\n"
                        f"RivaL usage: **{quota['requests_today']}/"
                        f"{quota['rival_daily_limit']}** requests today "
                        "(not Google's account-wide limit)\n"
                        f"{state}"
                    )[:1024],
                    inline=False,
                )
            except Exception:  # noqa: BLE001 - no secrets in UI
                _LOG.warning("RivaL Gemini quota status read unavailable")
                embed.add_field(
                    name="Gemini model & request limits",
                    value="The stored request budget could not be read. "
                          "No Google API request was made to check it.",
                    inline=False,
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
                        f"Gemini **{self.ai.model}** is configured for "
                        "on-demand, cached analysis."
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
