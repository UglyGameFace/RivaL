"""RivaL's private dual-track research menu. No listicle of ten feeds."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
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
        dashboard = self.view
        dashboard.asset = self.values[0]
        dashboard.opinion = None
        dashboard.message = None
        await dashboard.redraw(interaction)


class _ScreenButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self, screen: str) -> None:
        super().__init__(
            label={"math": "Math & History", "ai": "Gemini AI",
                   "accuracy": "Accuracy"}[screen],
            emoji={"math": "📐", "ai": "✨", "accuracy": "📊"}[screen],
            style=discord.ButtonStyle.primary,
            custom_id=f"rival:research:{screen}", row=0,
        )
        self.screen = screen

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        dashboard = self.view
        dashboard.screen = self.screen
        dashboard.message = None
        await dashboard.redraw(interaction)


class _RefreshButton(discord.ui.Button["ResearchDashboard"]):
    """Re-read current persisted source health and market pulse, without API calls."""

    def __init__(self) -> None:
        super().__init__(
            label="Update live stats", emoji="🔄",
            style=discord.ButtonStyle.primary,
            custom_id="rival:research:refresh", row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard._history_cache.clear()
        await dashboard.redraw(interaction)


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
        # Six-month inventory is an expensive historical aggregate. Cache it
        # briefly per asset; ALWAYS read live source health and market pulse
        # from PostgreSQL on every navigation/refresh.
        self._history_cache: dict[str, tuple[datetime, dict]] = {}
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
        for name in ("math", "ai", "accuracy"):
            item = _ScreenButton(name)
            # A selected tab is still clickable. Gray/disabled made users
            # think the entire research engine was unavailable.
            item.disabled = False
            item.style = (
                discord.ButtonStyle.success if self.screen == name
                else discord.ButtonStyle.primary
            )
            self.add_item(item)
        self.add_item(_AssetSelect(self.asset))
        self.add_item(_RefreshButton())
        if self.screen == "ai":
            enabled = self.market is not None and self.ai is not None and self.ai.available
            self.add_item(_AskButton(enabled))
            self.add_item(_ModelListButton(enabled=enabled))

    def _history_inventory_cached(self) -> dict:
        """Historical totals cached for 2 minutes, NEVER live feed status."""
        now = datetime.now(UTC)
        cached = self._history_cache.get(self.asset)
        if cached is not None and now - cached[0] < timedelta(minutes=2):
            return cached[1]
        assert self.market is not None
        value = history_inventory(
            self.market, asset=self.asset, venues=self.venues,
        )
        self._history_cache[self.asset] = (now, value)
        return value

    def _live_feed_status(
        self,
    ) -> tuple[int, int, datetime | None, datetime | None]:
        if self.market is None:
            return (0, len(self.venues), None, None)
        now = datetime.now(UTC)
        rows = self.market.feed_health()
        observed = [
            row for row in rows if row["asset"] == self.asset
            and row["venue"] in self.venues
        ]
        fresh = []
        recent_closes = []
        for row in observed:
            if row["status"] != "ok" or not row.get("last_success_at"):
                continue
            try:
                stamp = datetime.fromisoformat(str(row["last_success_at"]))
                if stamp.utcoffset() is None:
                    continue
                if not timedelta(0) <= now - stamp <= timedelta(minutes=10):
                    continue
                # A successful HTTP sync is not proof of current candles.
                # Old rows will become verifiable on the next collector cycle.
                closed_raw = row.get("last_candle_close")
                if not closed_raw:
                    continue
                closed = datetime.fromisoformat(str(closed_raw))
                if closed.utcoffset() is None:
                    continue
                if timedelta(0) <= now - closed <= timedelta(minutes=10):
                    fresh.append(row)
                    recent_closes.append(closed.astimezone(UTC))
            except (TypeError, ValueError):
                continue
        attempts = []
        for row in observed:
            try:
                when = datetime.fromisoformat(str(row["last_attempt_at"]))
                if when.utcoffset() is not None and when <= now:
                    attempts.append(when.astimezone(UTC))
            except (TypeError, ValueError):
                continue
        return (
            len({r["venue"] for r in fresh}),
            len(self.venues), max(attempts, default=None),
            max(recent_closes, default=None),
        )

    async def render(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"RivaL  |  {self.asset} Research",
            description=(
                "**Two independent tracks** · No validated betting edge. "
                "Market collection continues regardless of Gemini."
            ),
            color=discord.Color.green(),
        )
        # Source health is NOT cached: every menu edit reads actual
        # PostgreSQL collection status. "Live" means fresh recent polling,
        # not a tick-by-tick exchange order book stream.
        try:
            live, configured, checked, last_close = await asyncio.to_thread(
                self._live_feed_status,
            )
            stamp = (
                discord.utils.format_dt(checked, style="R")
                if checked is not None else "not yet"
            )
            bar = (
                discord.utils.format_dt(last_close, style="R")
                if last_close is not None else "unavailable"
            )
            embed.add_field(
                name="🟢 Live collection status",
                value=(
                    f"**{live}/{configured}** exchanges with recently saved "
                    f"{self.asset} candles. "
                    f"Last source check: **{stamp}**. "
                    f"Newest completed candle: **{bar}**.\n"
                    "Database read: **just now**. Values update on navigation "
                    "or **Update live stats**; exchange polling runs every 5m, "
                    "not tick-by-tick."
                ), inline=False,
            )
        except Exception:  # noqa: BLE001 - collection may be down
            _LOG.exception("RivaL live status read failed")
            embed.add_field(
                name="Live collection status",
                value="Current PostgreSQL collection status unavailable.",
                inline=False,
            )
        embed.timestamp = datetime.now(UTC)

        if self.screen == "math":
            if self.market is None:
                embed.add_field(
                    name="Mathematical analysis",
                    value="PostgreSQL collector unavailable.", inline=False,
                )
            else:
                try:
                    status = await asyncio.to_thread(
                        self._history_inventory_cached,
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
                except InsufficientEvidence as exc:
                    embed.add_field(
                        name="Current market pulse",
                        value=(
                            f"NO SIGNAL · {exc} "
                            "Exchange collection can be healthy while "
                            "the required continuous five-minute history "
                            "is still incomplete. Tap **Update live stats**."
                        )[:1024], inline=False,
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

        if self.screen == "accuracy":
            embed.description = (
                "**Prospective accuracy ledger** · RivaL records shadow "
                "predictions before future candles exist, then settles them "
                "from the SAME exchange after outcomes are observed. "
                "No live bets or automated trading."
            )
            if self.market is None or not self.shadow_enabled:
                embed.add_field(
                    name="Accuracy tracking",
                    value="Forward shadow evaluation is disabled or PostgreSQL "
                          "is unavailable. Accuracy cannot be established.",
                    inline=False,
                )
            else:
                try:
                    report = await asyncio.to_thread(
                        ForwardAccuracyMonitor(self.market).summary,
                        asset=self.asset,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL prospective score read unavailable")
                    embed.add_field(
                        name="Accuracy tracking",
                        value="The forward prediction ledger is temporarily "
                              "unavailable. Market collection continues.",
                        inline=False,
                    )
                else:
                    for horizon in (5, 15):
                        stats = report["last_7d"][horizon]
                        earlier = report["prior_7d"][horizon]
                        n = stats["settled"]
                        if n == 0:
                            value = (
                                f"Issued: **{stats['issued']}** · settled: **0**. "
                                "No measurable accuracy yet."
                            )
                        else:
                            hit = (
                                f"{stats['hit_rate'] * 100:.1f}%"
                                if stats["hit_rate"] is not None else "unavailable"
                            )
                            value = (
                                f"Issued: **{stats['issued']}**, settled: "
                                f"**{n}** across {stats['venues']} venues.\n"
                                f"Directional hit rate: **{hit}**.\n"
                                f"Brier loss: **{stats['brier']:.4f}** vs "
                                f"baseline **{stats['baseline_brier']:.4f}** "
                                "(lower is better)."
                            )
                            if stats["model_beats_baseline"] is None:
                                value += (
                                    "\nNot enough completed trials to "
                                    "judge predictive skill (minimum 50)."
                                )
                            else:
                                value += (
                                    "\nCandidate "
                                    + ("beats" if stats["model_beats_baseline"]
                                       else "does not beat")
                                    + " baseline in this window."
                                )
                        if earlier["settled"] >= 50 and n >= 50:
                            value += (
                                f"\nPrevious non-overlapping 7 days: "
                                f"{earlier['settled']} settled; "
                                f"Brier {earlier['brier']:.4f} vs "
                                f"{stats['brier']:.4f} now."
                            )
                        else:
                            value += (
                                "\nAccuracy trend: awaiting comparable "
                                "previous/current 7-day cohorts."
                            )
                        embed.add_field(
                            name=f"Forward {horizon}-minute evaluation",
                            value=value[:1024], inline=False,
                        )
                    embed.add_field(
                        name="How accuracy is audited",
                        value=(
                            "These are future-outcome scores, not historical "
                            "backfill wins. Each forecast is immutable and "
                            "settled once. Exchange returns are correlated, "
                            "so summed scores are NOT independent bets. "
                            "A better score on a small sample doesn't prove "
                            "a profitable edge after fees, funding or slippage."
                        ), inline=False,
                    )
            embed.set_footer(
                text="Live PostgreSQL audit • New outcomes accrue over time • "
                     "No accuracy guarantee"
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
