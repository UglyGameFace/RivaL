"""RivaL's private dual-track research menu. No listicle of ten feeds."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.opportunities import crypto_desk_embed
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence
from parlay_bot.perpetuals.analytics import analytics_snapshot
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.forward_tracking import (
    GOALS,
    ForwardTracking,
    TrackingUnavailable,
    tracking_text,
)
from parlay_bot.perpetuals.ai_types import AiResearch, ResearchUnavailable
from parlay_bot.perpetuals.history_inventory import history_inventory
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.local_first_research import LocalFirstResearch
from parlay_bot.perpetuals.market_pulse import calculate_market_pulse
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.proof_progress import proof_progress, proof_progress_text
from parlay_bot.perpetuals.readiness import explain_venue_readiness, forecast_readiness
from parlay_bot.perpetuals.shadow_monitor import MIN_EVALUATED, ShadowLearningMonitor
from parlay_bot.perpetuals.spike_watch import SpikeWatch
from parlay_bot.research.source_native import VENUES, source_month_readiness

if TYPE_CHECKING:
    from parlay_bot.discord_app.targeting import DirectionTargetService

_LOG = logging.getLogger(__name__)

# Every page has a hard content cap. Detailed metrics stay available behind a
# selector; no Discord message ever lists all research diagnostics at once.
_SECTIONS = {
    "opportunities": (
        ("overview", "Current watch"),
        ("proof", "Verified results"),
    ),
    "math": (
        ("overview", "Overview"),
        ("pulse", "Market pulse"),
        ("spikes", "Spikes & volume"),
        ("history", "History & gaps"),
        ("recovery", "Backfill progress"),
        ("learning", "Learning status"),
        ("ai", "AI Review"),
    ),
    "accuracy": (
        ("overview", "At a glance"),
        ("engine", "Is the engine running?"),
        ("readiness", "Can RivaL predict yet?"),
        ("progress", "Is any model proven better?"),
        ("tracker", "Track next predictions"),
        ("lineage", "Is it learning from history?"),
        ("venue", "Which exchange is blocked?"),
        ("five", "5-minute predictions"),
        ("fifteen", "15-minute predictions"),
        ("calibration", "Are probabilities reliable?"),
        ("ledger", "Recent predictions"),
        ("regimes", "Skill in different markets"),
        ("costs", "Profit after trading fees"),
        ("method", "How results are checked"),
        ("technical", "Technical evidence"),
    ),
    "ai": (
        ("overview", "Overview"),
        ("limits", "Intelligence status"),
        ("analysis", "AI assessment"),
    ),
}
_FIELD_GROUPS = {
    "math": {
        "ai": ("AI research · UNCALIBRATED", "Analysis status",
               "Private LFM3 Intelligence", "Intelligence status"),
        "pulse": ("Current market pulse",),
        "spikes": ("Spike watch analytics", "Recent spike events"),
        "history": ("Source-native training readiness", "Historical gap audit"),
        "recovery": ("Historical recovery progress",),
        "learning": ("Mathematical model", "Automatic shadow evaluation",
                     "Automatic learning"),
    },
    "accuracy": {
        "engine": ("Automatic learning engine", "Forward issue audit"),
        "readiness": ("Mathematical forecast readiness",),
        "progress": ("Future model proof",),
        "tracker": ("Prediction tracking",),
        "lineage": ("How is RivaL learning?", "Are advanced models ready?"),
        "technical": ("Model training actually used", "Boosted model production proof"),
        "venue": ("Why this exchange",),
        "five": ("Forward 5-minute evaluation", "Math 5m"),
        "fifteen": ("Forward 15-minute evaluation", "Math 15m"),
        "calibration": ("Forward 5m calibration", "Forward 15m calibration"),
        "ledger": ("Prediction ledger · prospective",),
        "regimes": ("Prospective skill by market regime",),
        "costs": ("Execution cost verification", "How accuracy is audited"),
        "method": ("How accuracy is audited",),
    },
    "ai": {
        "limits": ("Private LFM3 Intelligence", "Intelligence status"),
        "analysis": ("AI research · UNCALIBRATED", "Analysis status"),
    },
}


class _SectionSelect(discord.ui.Select["ResearchDashboard"]):
    def __init__(self, screen: str, section: str) -> None:
        super().__init__(
            placeholder="Choose what to see",
            custom_id="rival:research:section",
            row=3,
            options=[
                discord.SelectOption(
                    label=label, value=key, default=key == section,
                )
                for key, label in _SECTIONS[screen]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.section = self.values[0]
        await dashboard.redraw(interaction)


def _short(value: str, limit: int = 225) -> str:
    """Trim verbose boilerplate ONLY in summaries; details retain full text."""
    compact = " ".join(value.replace("\\n", "\n").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit - 1].rsplit(" ", 1)[0] + "…"


def _compact_embed(full: discord.Embed, *, screen: str, section: str) -> discord.Embed:
    """A real phone-sized screen; detailed metrics are separate screens."""
    label = dict(_SECTIONS[screen]).get(section, "Overview")
    result = discord.Embed(
        title=full.title,
        description={
            "math": "Live market observations and historical research. No approved trade.",
            "accuracy": "Predictions checked against what really happened. Profit is not yet proven.",
            "ai": "Read-only AI research on source-verified market data.",
        }[screen] + f"\n**{label}** · Choose a section below for details.",
        color=full.color,
    )
    result.timestamp = full.timestamp
    fields = list(full.fields)

    # Live health is always visible and never served from the history cache.
    source = next(
        (f for f in fields if "Live collection status" in f.name), None,
    )
    if source is not None:
        result.add_field(
            name="Live market feeds",
            value=_short(source.value, 180), inline=False,
        )

    def add_one(prefixes: tuple[str, ...], *, short: bool = False) -> bool:
        for prefix in prefixes:
            field = next((f for f in fields if f.name.startswith(prefix)), None)
            if field is None:
                continue
            result.add_field(
                name=field.name,
                value=_short(field.value, 230) if short else field.value,
                inline=False,
            )
            return True
        return False

    if section == "overview":
        if screen == "math":
            add_one(("Current market pulse",), short=True)
            add_one(("Six-month history target", "History"), short=True)
        elif screen == "accuracy":
            add_one(("Tonight's research verdict", "Accuracy tracking"), short=True)
            add_one(("Forward 5-minute evaluation",), short=True)
        else:
            add_one(("AI research · UNCALIBRATED", "Analysis status"), short=True)
            add_one(("Private local AI & Cloudflare backup",
                     "AI provider & request limits"), short=True)
    else:
        for prefix in _FIELD_GROUPS[screen].get(section, ()):
            if len(result.fields) >= 3:  # live + max two detailed metrics
                break
            add_one((prefix,))
    if len(result.fields) == (1 if source is not None else 0):
        result.add_field(
            name="No results yet",
            value="No verified records for this section yet. "
                  "The collector continues independently.",
            inline=False,
        )
    result.set_footer(
        text="Current PostgreSQL snapshot • Update live stats to reload • "
             "Historical results ≠ verified trading edge"
    )
    return result



def _plain_training_line(proof: dict, horizon: int) -> str:
    """Training activity is not a measured prediction win."""
    label = f"**{horizon}-minute predictions:**"
    if not proof["forecasts"]:
        return f"{label} No recorded predictions to check yet."
    if not proof["latest_lineage_verified"]:
        return (f"{label} Cannot confirm the latest prediction's training. "
                f"Proof missing on {proof['lineage_unknown']}/{proof['forecasts']} records.")
    status = (
        "Verified historical training used in the latest prediction."
        if proof["latest_historical_seed_sha256"] else
        "No verified historical training used in the latest prediction."
    )
    return (
        f"{label} {status} "
        f"{int(proof['latest_online_training_updates']):,} new-data learning updates; "
        f"{int(proof['historically_seeded'])}/{int(proof['lineage_proven'])} "
        "checked predictions used verified history."
    )


def _plain_boosted_proof(proof: dict) -> str:
    """Summarize boosted model screening without implying actual returns."""
    reasons = {
        "fewer_than_six_source_months": "not enough verified historical months",
        "insufficient_purged_validation_or_holdout": "not enough independent test examples",
        "validation_brier_gain_below_0.005": "did not improve enough over basic forecasts in validation",
        "holdout_brier_gain_below_0.005": "not better than a basic forecast on unseen tests",
        "calibration_or_evaluation_days_below_gate": "not enough days of reliable tests",
        "reason_unrecorded": "reason not recorded",
    }
    blockers = proof.get("screening_blockers") or []
    failures = "; ".join(
        f"{int(row['count'])} {reasons.get(row['reason'], 'failed another check')}"
        for row in blockers[:2]
    ) or "none recorded"
    paper = len(proof["fresh_paper_promotions"])
    verdict = (
        "Some qualify for simulated testing, not real trades." if paper
        else "No model qualifies for simulated testing yet."
    )
    return (
        f"**Advanced models imported:** {int(proof['portable_imported']):,}. "
        f"**Screened:** {int(proof['portable_screened']):,}.\n"
        f"**What's holding them back:** {failures}.\n"
        f"**Predictions logged before outcomes (30 days):** {int(proof['issued']):,}; "
        f"**results checked:** {int(proof['settled']):,}; "
        f"**exit prices checked:** {int(proof['quote_exits']):,}.\n"
        f"**Ready for practice-only tests:** {paper}. {verdict}\n"
        "Real profit after trading fees, funding and slippage is not verified."
    )


class _AssetSelect(discord.ui.Select["ResearchDashboard"]):
    def __init__(self, current: str) -> None:
        super().__init__(
            placeholder="Choose a coin",
            options=[
                discord.SelectOption(label=a, value=a, default=a == current)
                for a in ("BTC", "ETH", "SOL", "XRP")
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


class _VenueSelect(discord.ui.Select["ResearchDashboard"]):
    """Inspect a single source's causal forecast blocker; no giant source list."""

    def __init__(self, venues: tuple[str, ...], selected: str) -> None:
        super().__init__(
            placeholder="Inspect an exchange's forecast readiness",
            custom_id="rival:research:venue", row=4,
            options=[
                discord.SelectOption(
                    label=venue.replace("_", " ").title(),
                    value=venue, default=venue == selected,
                )
                for venue in venues[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.selected_venue = self.values[0]
        await dashboard.redraw(interaction)


class _TrackStartSelect(discord.ui.Select["ResearchDashboard"]):
    """Explicit opt-in cohorts that survive Discord navigation and bot restarts."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Start tracking the NEXT predictions",
            custom_id="rival:research:track-start",
            row=4,
            options=[
                discord.SelectOption(
                    label=f"Next {goal} forecasts · {horizon} minutes",
                    value=f"{horizon}:{goal}",
                )
                for horizon in (5, 15, 60) for goal in GOALS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        await interaction.response.defer()
        if dashboard.market is None or not dashboard.selected_venue:
            dashboard.message = "No saved exchange feed available for tracking."
        else:
            horizon, target = map(int, self.values[0].split(":"))
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        ForwardTracking(dashboard.market.store).start,
                        owner_id=dashboard.owner_id, asset=dashboard.asset,
                        venue=dashboard.selected_venue,
                        horizon=horizon, target=target,
                    ),
                    timeout=8,
                )
                dashboard.message = (
                    f"Tracking next {result['target']} predictions "
                    f"({horizon}m) from {dashboard.selected_venue}."
                    if result["status"] == "started" else
                    "A previous tracking session is still active for this "
                    "exchange and timeframe. It will not be reset."
                )
            except TrackingUnavailable as exc:
                dashboard.message = str(exc)
            except Exception:  # noqa: BLE001 - preserve original Discord controls
                _LOG.exception("Could not start RivaL future-tracking session")
                dashboard.message = (
                    "Could not record this session. No historical predictions "
                    "were counted or tracked retroactively."
                )
        await dashboard.redraw(interaction, acknowledged=True)


class _TrackVenueButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self):
        super().__init__(
            label="Choose exchange", emoji="🔎",
            custom_id="rival:research:track-venue",
            style=discord.ButtonStyle.secondary, row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.section = "progress"
        await dashboard.redraw(interaction)


class _ScreenButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self, screen: str) -> None:
        super().__init__(
            label={"opportunities": "Opportunities", "math": "Math & History",
                   "ai": "AI Review", "accuracy": "Results"}[screen],
            emoji={"opportunities": "🎯", "math": "📐",
                   "ai": "✨", "accuracy": "📊"}[screen],
            style=discord.ButtonStyle.primary,
            custom_id=f"rival:research:{screen}", row=0,
        )
        self.screen = screen

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        dashboard = self.view
        dashboard.screen = self.screen
        dashboard.section = "overview"
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
        if dashboard.section in ("history", "recovery"):
            dashboard._history_cache.pop(dashboard.asset, None)
        await dashboard.redraw(interaction)


class _QuickTargetButton(discord.ui.Button["ResearchDashboard"]):
    """One-tap personal watch for the currently selected coin."""

    def __init__(self) -> None:
        super().__init__(
            label="Follow 5m UP/DOWN", emoji="🎯",
            style=discord.ButtonStyle.success, row=2,
            custom_id="rival:research:follow-target",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        if dashboard.target_service is None:
            await interaction.followup.send(
                "Personal monitoring is not configured.", ephemeral=True,
            )
            return
        try:
            ok, message = await dashboard.target_service.follow(
                interaction.user, asset=dashboard.asset, horizon_minutes=5,
            )
        except Exception:  # noqa: BLE001 - never expose internal errors
            _LOG.exception("RivaL 5m target follow failed")
            ok, message = False, "Could not start monitoring. Try /rival-target."
        await interaction.followup.send(
            ("✅ " if ok else "⛔ ") + message, ephemeral=True,
        )


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
        dashboard.screen = "math"
        dashboard.section = "ai"
        dashboard.message = None
        dashboard.opinion = None
        try:
            if dashboard.ai is None:
                raise ResearchUnavailable("AI review is unavailable.")
            dashboard.opinion = await dashboard.ai.ask(
                asset=dashboard.asset, venues=dashboard.venues,
                requester_id=interaction.user.id,
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
        ai: LocalFirstResearch | None,
        venues: tuple[str, ...],
        shadow_enabled: bool = False,
        target_service: DirectionTargetService | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.market = market
        self.ai = ai
        self.venues = venues
        self.shadow_enabled = shadow_enabled
        self.target_service = target_service
        self.asset = "BTC"
        self.screen = "opportunities"
        self.section = "overview"
        self.selected_venue = venues[0] if venues else ""
        self.opinion: AiResearch | None = None
        self.message: str | None = None
        # Six-month inventory is an expensive historical aggregate. Cache it
        # briefly per asset; ALWAYS read live source health and market pulse
        # from PostgreSQL on every navigation/refresh.
        self._history_cache: dict[str, tuple[datetime, dict]] = {}
        # Shared across successive message generations. The newest click wins,
        # so an old slow database response cannot repaint a newer selection.
        self._navigation_state: dict[str, object] = {"revision": 0, "pending": None}
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
        for name in ("opportunities", "math", "accuracy"):
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
        self.add_item(_SectionSelect(self.screen, self.section))
        if self.screen == "accuracy" and self.section == "tracker":
            self.add_item(_TrackStartSelect())
            self.add_item(_TrackVenueButton())
        if self.screen == "opportunities" and self.target_service is not None:
            self.add_item(_QuickTargetButton())
        if (((self.screen == "accuracy" and self.section == "venue")
             or (self.screen == "math" and self.section == "history")
              or (self.screen == "accuracy" and self.section == "progress"))
                and self.venues):
            self.add_item(_VenueSelect(self.venues, self.selected_venue))
        if self.screen == "ai" or (self.screen == "math" and self.section == "ai"):
            enabled = self.market is not None and self.ai is not None and self.ai.available
            self.add_item(_AskButton(enabled))

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

    def _native_month_status(self) -> dict:
        """The selected actual exchange, never combined Binance history."""
        from parlay_bot.perpetuals.history_quality import _market

        assert self.market is not None
        if self.selected_venue not in VENUES:
            raise ValueError("This venue has no eligible native research stream")
        contract, quote = _market(self.selected_venue, self.asset)
        return source_month_readiness(
            self.market, venue=self.selected_venue,
            contract=contract, quote=quote,
        )

    def _history_quality_status(self) -> tuple[list[dict], dict | None]:
        """Read latest completed gap audit and separate recovery progress."""
        if self.market is None:
            return [], None
        audits = SixMonthQualityAudit(self.market).latest(
            asset=self.asset, venues=self.venues,
        )
        recovery = None
        with self.market.store.connect() as conn:
            try:
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='gate' AND asset=?""",
                    (self.asset,),
                ).fetchone()
            except Exception:  # noqa: BLE001 - older DB may not have this table
                row = None
            if row is not None:
                recovery = dict(row)
        return audits, recovery

    def _kucoin_history_progress(self) -> dict | None:
        """Read KuCoin-specific recovery progress without fetching exchange data."""
        if self.market is None:
            return None
        with self.market.store.connect() as conn:
            try:
                row = conn.execute(
                    """SELECT status,inserted_bars,next_opened_at,empty_windows,
                              attempted_at
                       FROM perp_history_recovery
                       WHERE venue='kucoin' AND asset=?""",
                    (self.asset,),
                ).fetchone()
            except Exception:  # noqa: BLE001 - older DB may not have schema
                return None
        return dict(row) if row is not None else None

    def _okx_history_progress(self) -> dict | None:
        """Reverse-chronological OKX six-month repair state from PostgreSQL."""
        if self.market is None:
            return None
        with self.market.store.connect() as conn:
            try:
                row = conn.execute(
                    """SELECT status,inserted_bars,next_opened_at,empty_windows,
                              attempted_at
                       FROM perp_history_recovery
                       WHERE venue='okx' AND asset=?""",
                    (self.asset,),
                ).fetchone()
            except Exception:  # noqa: BLE001 - previous DB version
                return None
        return dict(row) if row is not None else None

    def _bitget_history_progress(self) -> dict | None:
        """One DB query, no Bitget API request or stale embedded cache."""
        if self.market is None:
            return None
        with self.market.store.connect() as conn:
            try:
                row = conn.execute(
                    """SELECT status,inserted_bars,next_opened_at,empty_windows,
                              attempted_at
                       FROM perp_history_recovery
                       WHERE venue='bitget' AND asset=?""",
                    (self.asset,),
                ).fetchone()
            except Exception:  # noqa: BLE001 - schema upgrade pending
                return None
        return dict(row) if row is not None else None

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
        if self.screen == "opportunities":
            return await asyncio.to_thread(
                crypto_desk_embed, self.market, asset=self.asset,
                proof=self.section == "proof",
            )
        detailed = await self._render_full()
        return _compact_embed(
            detailed, screen=self.screen, section=self.section,
        )

    async def _render_full(self) -> discord.Embed:
        """Current authoritative data, never returned as a giant Discord embed."""
        embed = discord.Embed(
            title=f"RivaL  |  {self.asset} Research",
            description=(
                "**Two independent tracks** · No validated betting edge. "
                "Market collection continues independently of AI requests."
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
                    f"**{live}/{configured}** exchanges have recent {self.asset} prices.\n"
                    f"**Last update check:** {stamp}. "
                    f"**Newest finished price update:** {bar}.\n"
                    "Exchange prices refresh about every 5 minutes, "
                    "not continuously."
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

        if self.screen == "math" and self.section != "ai":
            if self.section == "history" and self.market is not None:
                try:
                    state = await asyncio.to_thread(self._native_month_status)
                    monthly = state["months"]
                    observations = " · ".join(
                        f"{m['month'][5:]}: "
                        + ("verified" if m["status"] == "published_receipt"
                           else "count only" if m["status"] == "count_only_not_certified"
                           else f"{m['bars']:,}/{m['expected_bars']:,}")
                        for m in monthly
                    )
                    embed.add_field(
                        name="Source-native training readiness",
                        value=(
                            f"**{state['venue']} / {state['contract']} ({state['quote']})**\n"
                            f"Published source-month receipts: "
                            f"**{state['months_with_receipts']}/6**.\n"
                            f"{observations}\n"
                            + ("Receipt table unavailable; status is unknown. "
                               if not state["receipt_read_available"] else "")
                            + "Count-only months are NOT certified. "
                              "Training still requires private Drive SHA-256 "
                              "restoration; publication and future model proof are separate."
                        )[:1024], inline=False,
                    )
                except ValueError:
                    embed.add_field(
                        name="Source-native training readiness",
                        value="Select a supported live exchange to inspect exact "
                              "monthly source training evidence.",
                        inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL monthly source research evidence unavailable")
                    embed.add_field(
                        name="Source-native training readiness",
                        value="Host monthly coverage could not be verified. "
                              "No source-native model training is proven.",
                        inline=False,
                    )
            if self.section in ("overview", "history", "recovery"):
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
                        if self.section in ("history", "recovery"):
                            try:
                                audits, recovery = await asyncio.to_thread(
                                    self._history_quality_status,
                                )
                                if audits:
                                    complete = sum(x["status"] == "complete" for x in audits)
                                    missing = sum(int(x["missing_bars"]) for x in audits)
                                    anomalies = sum(
                                        int(x["off_grid_bars"]) +
                                        int(x["invalid_observations"])
                                        for x in audits
                                    )
                                    last = max(
                                        datetime.fromisoformat(x["audited_at"])
                                        for x in audits
                                    )
                                    embed.add_field(
                                        name="Historical gap audit",
                                        value=(
                                            f"**{complete}/{len(audits)}** inspected streams "
                                            f"have complete six-month history; "
                                            f"**{missing:,}** missing bars and "
                                            f"**{anomalies:,}** malformed/invalid observations "
                                            f"across last completed per-venue audits. "
                                            f"Last check {discord.utils.format_dt(last, style='R')}. "
                                            "This measures real continuity, not model accuracy."
                                        ), inline=False,
                                    )
                                else:
                                    embed.add_field(
                                        name="Historical gap audit",
                                        value="No completed background six-month continuity audit yet.",
                                        inline=False,
                                    )
                                if recovery is not None:
                                    embed.add_field(
                                        name="Gate six-month recovery",
                                        value=(
                                            f"**{recovery['status']}** · "
                                            f"**{int(recovery['inserted_bars']):,}** old "
                                            f"candles imported for {self.asset}. "
                                            f"Cursor: {str(recovery['next_opened_at'])[:16]} UTC. "
                                            "No old candle is counted as a live prediction."
                                        ), inline=False,
                                    )
                                bitget = await asyncio.to_thread(
                                    self._bitget_history_progress,
                                )
                                kucoin = await asyncio.to_thread(
                                    self._kucoin_history_progress,
                                )
                                if kucoin is not None:
                                    embed.add_field(
                                        name="KuCoin six-month recovery",
                                        value=(
                                            f"**{kucoin['status']}** · "
                                            f"**{int(kucoin['inserted_bars']):,}** old "
                                            f"candles imported for {self.asset}. "
                                            f"Cursor: {str(kucoin['next_opened_at'])[:16]} UTC. "
                                            f"Missing intervals: **{int(kucoin['empty_windows'])}** "
                                            "empty request windows. "
                                            "These are NOT live forward tests."
                                        )[:1024], inline=False,
                                    )
                                okx = await asyncio.to_thread(
                                    self._okx_history_progress,
                                )
                                progress = (
                                    ("Gate", recovery), ("KuCoin", kucoin),
                                    ("Bitget", bitget), ("OKX", okx),
                                )
                                lines = [
                                    (
                                        f"**{name}**: {row['status']} · "
                                        f"{int(row['inserted_bars']):,} imported · "
                                        f"cursor {str(row['next_opened_at'])[:16]} UTC"
                                    ) if row is not None else f"**{name}**: not started"
                                    for name, row in progress
                                ]
                                embed.add_field(
                                    name="Historical recovery progress",
                                    value="\n".join(lines) +
                                          "\nBackfills are NOT past live predictions. "
                                          "Continuity audits verify actual gaps.",
                                    inline=False,
                                )
                            except Exception:  # noqa: BLE001
                                _LOG.exception("History audit status read unavailable")
                                embed.add_field(
                                    name="Historical gap audit",
                                    value="Gap-check report temporarily unavailable.",
                                    inline=False,
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
            if self.section in ("overview", "pulse") and self.market is not None:
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
            if self.section == "spikes":
                try:
                    spike = await asyncio.to_thread(
                        SpikeWatch(self.market).snapshot, asset=self.asset,
                    )
                    events = spike.get("events_24h")
                    embed.add_field(
                        name="Spike watch analytics",
                        value=(
                            f"Status: **{spike['status'].replace('_', ' ')}** · "
                            f"Fresh venues: **{spike.get('fresh_venues', 0)}**.\\n"
                            f"Last scan: **{spike.get('checked_at') or 'not yet'}**.\\n"
                            f"Past 24h unusual events: **"
                            f"{events if events is not None else 'unknown'}** · "
                            f"confirmed continuations: **{spike.get('scored_continuations_24h') or 0}** "
                            f"· reversals: **{spike.get('scored_reversals_24h') or 0}** "
                            f"· source-unverified: **{spike.get('unverified_24h') or 0}**.\\n"
                            "Future outcomes update automatically, never backfilled "
                            "as live scores; no validated trade signal."
                        )[:1024], inline=False,
                    )
                    lines = [
                        f"{r['opened_at'][5:16]} UTC · {r['kind']} "
                        f"{r['direction']} {r['move_pct']:+.2f}% · "
                        f"{r['source_count']} venues · "
                        f"{r['outcome'] or 'awaiting future'}"
                        for r in spike.get("recent", [])[:3]
                    ]
                    embed.add_field(
                        name="Recent spike events",
                        value="\\n".join(lines) if lines else
                              "No source-verified events recorded in 24h.",
                        inline=False,
                    )
                except Exception:  # noqa: BLE001 - keep Discord responsive
                    _LOG.exception("RivaL spike analytics unavailable")
                    embed.add_field(
                        name="Spike watch analytics",
                        value="Verified observation ledger currently unavailable.",
                        inline=False,
                    )
            if self.section == "learning":
                embed.add_field(
                    name="Mathematical model",
                    value=(
                        "Online weights are evaluated in the background on "
                        "settled observations. No model is approved for "
                        "production trading signals."
                    ) if self.shadow_enabled else (
                        "Automatic shadow evaluation is disabled; "
                        "no production model is approved."
                    ),
                    inline=False,
                )
            if self.section == "learning" and self.shadow_enabled:
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

        if self.screen == "accuracy" and self.section == "tracker":
            if self.market is None:
                text = "Database unavailable. No tracking can be checked."
            elif not self.selected_venue:
                text = "Choose a configured exchange before tracking."
            else:
                try:
                    result = await asyncio.to_thread(
                        ForwardTracking(self.market.store).report,
                        owner_id=self.owner_id, asset=self.asset,
                        venue=self.selected_venue,
                    )
                    text = tracking_text(result)
                except Exception:  # noqa: BLE001 - do not invent past forecasts
                    _LOG.exception("RivaL forward-tracking report unavailable")
                    text = "Could not read tracking records; no results inferred."
            if self.message:
                text = (f"{self.message}\n\n{text}")[:1024]
            embed.add_field(name="Prediction tracking", value=text, inline=False)
            embed.set_footer(
                text=f"Next predictions only · {self.selected_venue} · No real P&L"
            )
            return embed

        if self.screen == "accuracy" and self.section == "progress":
            if self.market is None:
                value = "Database unavailable. No model proof can be checked."
            else:
                try:
                    current = await asyncio.to_thread(
                        proof_progress, self.market.store, asset=self.asset,
                        venue=self.selected_venue,
                    )
                    value = proof_progress_text(current)
                except Exception:  # noqa: BLE001 - optional evidence fails closed
                    _LOG.exception("RivaL model proof progress read unavailable")
                    value = "Proof could not be checked. No qualification is assumed."
            embed.add_field(name="Future model proof", value=value, inline=False)
            embed.set_footer(
                text="Per-exchange future scores · No real fills or trading approval"
            )
            return embed

        if self.screen == "accuracy":
            # These independent proof pages must render even if a separate
            # analytics query is unavailable. No generic score can hide the
            # actual forecast ledger, regime status, or execution-cost gate.
            # Only request expensive, detailed proof when the user
            # actually opens that page. This is a fresh DB query, not
            # a 5-minute stale widget or an LLM-generated number.
            if self.section in ("lineage", "technical"):
                try:
                    monitor = ForwardAccuracyMonitor(self.market)
                    rows = []
                    plain_rows = []
                    for horizon in (5, 15, 60):
                        proof = await asyncio.to_thread(
                            monitor.learning_lineage_summary,
                            asset=self.asset, horizon_minutes=horizon,
                        )
                        plain_rows.append(_plain_training_line(proof, horizon))
                        if not proof["forecasts"]:
                            rows.append(f"**{horizon}m:** No issued forecasts to audit.")
                        elif not proof["latest_lineage_verified"]:
                            rows.append(
                                f"**{horizon}m:** Latest forecast predates training proof. "
                                f"{proof['lineage_unknown']} of "
                                f"{proof['forecasts']} lineage records unknown."
                            )
                        else:
                            seed = proof["latest_historical_seed_sha256"]
                            history = (
                                f"Verified historical seed `{seed[:10]}` "
                                f"({proof['latest_historical_training_labels']} labels)"
                                if seed else "No validated historical model used"
                            )
                            rows.append(
                                f"**{horizon}m:** {history}; "
                                f"**{proof['latest_online_training_updates']}** "
                                "online updates. "
                                f"{proof['historically_seeded']} of "
                                f"{proof['lineage_proven']} recorded forecasts "
                                "had a historical seed."
                            )
                    embed.add_field(
                        name=("How is RivaL learning?" if self.section == "lineage"
                              else "Model training actually used"),
                        value=(
                            ("\n".join(plain_rows) + "\nLearning updates are not prediction wins.")
                            if self.section == "lineage" else
                            ("\n".join(rows) + "\nTraining evidence is NOT a measured win rate. "
                             "An unapproved six-month archive does not count as live model training.")
                        )[:1024], inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL model training lineage unavailable")
                    embed.add_field(
                        name=("How is RivaL learning?" if self.section == "lineage"
                              else "Model training actually used"),
                        value="Training history cannot be checked right now. "
                              "Do not assume historical training was used.",
                        inline=False,
                    )
            if self.section in ("lineage", "technical"):
                try:
                    live_proof = await asyncio.to_thread(
                        ForwardAccuracyMonitor(self.market).boosted_production_evidence,
                        asset=self.asset,
                    )
                    recent = live_proof["scopes"][0] if live_proof["scopes"] else None
                    scope_label = (
                        f"{recent['venue']}/{recent['contract']} "
                        f"{recent['horizon']}m"
                        if recent else "None verified"
                    )
                    reasons = {
                        "fewer_than_six_source_months":
                            "fewer than 6 verified source months",
                        "insufficient_purged_validation_or_holdout":
                            "too few validation/holdout examples",
                        "validation_brier_gain_below_0.005":
                            "validation Brier edge below 0.005",
                        "holdout_brier_gain_below_0.005":
                            "holdout Brier edge below 0.005",
                        "calibration_or_evaluation_days_below_gate":
                            "calibration or evaluation days below gate",
                        "reason_unrecorded": "screening reason unavailable",
                    }
                    blockers = live_proof.get("screening_blockers") or []
                    explanation = (
                        "Screening blockers: " + "; ".join(
                            f"{int(row['count'])} "
                            f"{reasons.get(row['reason'], 'other screening gate')}"
                            for row in blockers[:2]
                        ) + ("; other reasons in registry" if len(blockers) > 2
                             else "")
                        if blockers else "Screening blockers: none recorded"
                    )
                    embed.add_field(
                        name=("Are advanced models ready?" if self.section == "lineage"
                              else "Boosted model production proof"),
                        value=(_plain_boosted_proof(live_proof) if self.section == "lineage" else (
                            f"**Imported:** {live_proof['portable_imported']} portable trees, "
                            f"**{live_proof['portable_screened']}** screened.\n"
                            f"{explanation}\n"
                            f"**Last 30 days:** {live_proof['issued']} immutable "
                            f"forecasts, {live_proof['settled']} future settlements, "
                            f"{live_proof['quote_exits']} matched exit book quotes.\n"
                            f"**Most recent source:** {scope_label}. "
                            f"**Paper-qualified:** "
                            f"{len(live_proof['fresh_paper_promotions'])}.\n"
                            f"**Verdict:** {live_proof['status_reason']}.\n"
                            "Real fills, fees, funding and slippage: **NOT VERIFIED**. "
                            "GitHub research artifacts are not imported live models."
                        ))[:1024],
                        inline=False,
                    )
                except Exception:  # noqa: BLE001 - source evidence fails closed
                    _LOG.exception("RivaL boosted production ledger read unavailable")
                    embed.add_field(
                        name=("Are advanced models ready?" if self.section == "lineage"
                              else "Boosted model production proof"),
                        value="Cannot check model approval right now. "
                              "No model readiness or profitability can be assumed.",
                        inline=False,
                    )
            if self.section == "ledger":
                try:
                    issued = await asyncio.to_thread(
                        ForwardAccuracyMonitor(self.market).recent_issued,
                        asset=self.asset, limit=3,
                    )
                    lines = []
                    for pred in issued:
                        when = datetime.fromisoformat(
                            str(pred["issued_at"])
                        )
                        if when.utcoffset() is None:
                            continue
                        done = (
                            "settled" if pred["settled_at"] else "awaiting outcome"
                        )
                        lines.append(
                            f"**{pred['horizon_minutes']}m** "
                            f"{pred['venue']} · P(up) "
                            f"**{float(pred['probability_up']):.3f}** · "
                            f"issued {discord.utils.format_dt(when, style='R')} "
                            f"· {done}"
                        )
                    embed.add_field(
                        name="Prediction ledger · prospective",
                        value=(
                            "\n".join(lines) if lines else
                            "No pre-outcome mathematical predictions "
                            "have been issued yet. Do not fabricate signals."
                        )[:1024],
                        inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL forward prediction ledger unavailable")
                    embed.add_field(
                        name="Prediction ledger · prospective",
                        value="Database proof unavailable; no forecast inferred.",
                        inline=False,
                    )
            if self.section == "regimes":
                try:
                    monitor = ForwardAccuracyMonitor(self.market)
                    pieces = []
                    for horizon in (5, 15):
                        proof = await asyncio.to_thread(
                            monitor.regime_summary,
                            asset=self.asset, horizon_minutes=horizon,
                        )
                        lines = []
                        for regime in ("quiet", "normal", "turbulent"):
                            part = proof["regimes"][regime]
                            gain = part["paired_brier_gain"]
                            gain_text = (
                                f"{gain:+.4f}" if gain is not None else "none"
                            )
                            lines.append(
                                f"{regime}: **{part['unique_targets']}** "
                                f"UTC targets / {part['utc_days']} days · "
                                f"paired Brier gain {gain_text}"
                            )
                        pieces.append(
                            f"**{horizon}m** · "
                            f"{proof['status'].replace('_', ' ')} "
                            f"({proof['disputed_regime_targets']} disputed)\n"
                            + "\n".join(lines)
                        )
                    embed.add_field(
                        name="Prospective skill by market regime",
                        value=(
                            "\n\n".join(pieces)
                            + "\nRegimes were stamped BEFORE outcomes. "
                              "Multiple venues count once per target. "
                              "A stable shadow score is not trading profit."
                        )[:1024], inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL regime-evidence query unavailable")
                    embed.add_field(
                        name="Prospective skill by market regime",
                        value="Regime proof unavailable. No improvement claimed.",
                        inline=False,
                    )
            if self.section == "costs":
                embed.add_field(
                    name="Execution cost verification",
                    value=(
                        "**NOT VERIFIED.** RivaL has no authenticated "
                        "matched entry/exit fill receipts for these "
                        "prospective forecasts, nor reconciled actual "
                        "entry+exit fees, arrival-to-fill slippage, "
                        "funding payments and liquidation costs. "
                        "The 16/40/80 bps stress scenarios are "
                        "assumptions, NOT measured execution costs. "
                        "Positive historical candle movement or "
                        "shadow-model skill cannot establish net "
                        "profitability. No live orders are placed."
                    ), inline=False,
                )
            # The selected evidence above is already fresh and source-specific.
            # Do not run every other accuracy, analytics and readiness query
            # for lineage, ledger, regime or cost screens. The old full-render
            # path did this despite showing at most two fields on a phone.
            if self.section in ("lineage", "technical", "ledger", "regimes", "costs"):
                embed.set_footer(text="Read-only database evidence • No trading approval")
                return embed

            if self.section == "engine":
                # Read-only proof that the autonomous engine actually attempted
                # its last scheduled research cycle. Do not confuse zero issued
                # forecasts with a failed learning process.
                if self.market is not None:
                    try:
                        latest = await asyncio.to_thread(
                            AutonomousResearch(
                                self.market, ai=None, venues=self.venues,
                            ).learning_heartbeat,
                        )
                        if latest is None:
                            heartbeat = (
                                "No automatic learning cycle has been recorded "
                                "since this feature was deployed. This is not "
                                "evidence of a trained model."
                            )
                        else:
                            completed = datetime.fromisoformat(
                                str(latest["completed_at"])
                            )
                            age = datetime.now(UTC) - completed.astimezone(UTC)
                            if age < timedelta(0):
                                health = "Invalid future timestamp"
                            elif age > timedelta(minutes=12):
                                health = "🔴 Stale: automatic research may be stalled"
                            elif any(not bool(latest[key]) for key in (
                                "math_ok", "ai_settlement_ok",
                                "model_review_ok", "gemini_ok",
                            )):
                                health = "🟡 Cycle ran, but a stage failed"
                            else:
                                health = "🟢 Background research cycle completed"
                            stages = (
                                "math " + ("OK" if latest["math_ok"] else "FAILED")
                                + " · settlement "
                                + ("OK" if latest["ai_settlement_ok"] else "FAILED")
                                + " · governance "
                                + ("OK" if latest["model_review_ok"] else "FAILED")
                                + " · optional AI scheduler "
                                + ("OK" if latest["gemini_ok"] else "FAILED")
                            )
                            heartbeat = (
                                f"**{health}** · "
                                f"{discord.utils.format_dt(completed, style='R')}\\n"
                                f"{stages}\\n"
                                f"Last cycle: **{latest['math_issued']}** math "
                                f"forecasts issued, **{latest['math_settled']}** "
                                "settled, "
                                f"**{latest['ai_issued']}** optional AI records.\\n"
                                "Zero means no new qualifying forecast, "
                                "**not** that prior predictions became more accurate."
                            ).replace("\\n", "\n")
                        embed.add_field(
                            name="Automatic learning engine · LIVE",
                            value=heartbeat[:1024], inline=False,
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("RivaL learning heartbeat read unavailable")
                        embed.add_field(
                            name="Automatic learning engine · LIVE",
                            value="PostgreSQL cycle diagnostics unavailable; "
                                  "do not assume the engine is running.",
                            inline=False,
                        )
                # A successful heartbeat with zero predictions is ambiguous until
                # we inspect the forecast gate outcomes from the *actual* run.
                # Query one small durable row; never infer readiness from candle totals.
                if self.market is not None:
                    try:
                        diagnostics = await asyncio.to_thread(
                            ForwardAccuracyMonitor(self.market).latest_diagnostics,
                        )
                        if diagnostics is None:
                            details = (
                                "No automatic issuance audit has been recorded yet. "
                                "Collection may be working, but forecast readiness "
                                "has not been verified."
                            )
                        else:
                            checked = datetime.fromisoformat(
                                str(diagnostics["checked_at"])
                            ).astimezone(UTC)
                            elapsed = datetime.now(UTC) - checked
                            state = (
                                "🔴 Stale execution"
                                if elapsed > timedelta(minutes=12)
                                else "🟢 Fresh execution"
                                if elapsed >= timedelta(0)
                                else "⚠️ Invalid future timestamp"
                            )
                            details = (
                                f"**{state}** · "
                                f"{discord.utils.format_dt(checked, style='R')}\\n"
                                f"Discovered **{diagnostics['discovered_streams']}** "
                                f"fresh streams, checked "
                                f"**{diagnostics['selected_streams']}** this cycle.\\n"
                                f"Issued **{diagnostics['issued_forecasts']}**, "
                                f"settled **{diagnostics['settled_forecasts']}**, "
                                f"already-issued **{diagnostics['duplicate_forecasts']}**.\\n"
                                f"Blocked by short history "
                                f"**{diagnostics['short_history']}**, "
                                f"stale **{diagnostics['stale_last_candle']}**, "
                                f"invalid **{diagnostics['invalid_data']}**; "
                                f"insufficient causal labels "
                                f"**{diagnostics['insufficient_causal_labels']}**, "
                                f"insufficient evaluation "
                                f"**{diagnostics['insufficient_training']}**, "
                                f"expired target **{diagnostics['target_expired']}**."
                            ).replace("\\n", "\n")
                        embed.add_field(
                            name="Forward issue audit · LIVE",
                            value=details[:1024],
                            inline=False,
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("RivaL forward issue diagnostics unavailable")
                        embed.add_field(
                            name="Forward issue audit · LIVE",
                            value="Cannot read forecast issue audit from PostgreSQL. "
                                  "Do not infer model health from candle counts.",
                            inline=False,
                        )
            embed.description = (
                "**Prospective accuracy ledger** · RivaL records shadow "
                "predictions before future candles exist, then settles them "
                "from the SAME exchange after outcomes are observed. "
                "No live bets or automated trading."
            )
            if self.section == "engine":
                # Engine/worker health is a distinct live read. It must not
                # depend on slow accuracy baselines or external AI analytics.
                embed.set_footer(text="Live cycle and issuance diagnostics • Not skill proof")
                return embed
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
                    if self.section in ("readiness", "venue"):
                        # Always recompute genuine live observation readiness from
                        # PostgreSQL, never reuse six-month historical cache.
                        try:
                            readiness = await asyncio.to_thread(
                                forecast_readiness, self.market,
                                asset=self.asset, venues=self.venues,
                            )
                        except Exception:  # noqa: BLE001
                            _LOG.exception("RivaL forecast readiness audit failed")
                            embed.add_field(
                                name="Mathematical forecast readiness · LIVE",
                                value="Current readiness audit unavailable; "
                                      "no forecast was inferred.",
                                inline=False,
                            )
                        else:
                            ready = readiness["eligible"]
                            configured = readiness["configured"]
                            streak = readiness["best_live_streak"]
                            blocker = (
                                "Eligible for shadow evaluation; verify actual "
                                "issued/settled forecasts below."
                                if ready else
                                "Collecting is not training: RivaL requires "
                                "continuous live-observed candles and outcomes "
                                "already known before model evaluation."
                            )
                            embed.add_field(
                                name="Mathematical forecast readiness · LIVE",
                                value=(
                                    f"**{ready}/{configured}** exchange streams "
                                    "eligible for 5m and 15m shadow evaluation.\n"
                                    f"Longest genuine live streak: "
                                    f"**{streak}/{readiness['min_live_bars']}** bars.\n"
                                    f"Best 5m causal samples: "
                                    f"**{readiness['best_causal_5m']}**, "
                                    f"evaluated **{readiness['best_evaluated_5m']}** "
                                    f"(minimum {readiness['min_evaluations']}).\n"
                                    f"Stale/blocked: **{readiness['stale_or_blocked']}** "
                                    f"· awaiting live history: "
                                    f"**{readiness['waiting_history']}**.\n"
                                    f"{blocker} No betting permission implied."
                                )[:1024], inline=False,
                            )
                            embed.add_field(
                                name="Why this exchange is or isn't ready",
                                value=explain_venue_readiness(
                                    readiness, self.selected_venue,
                                )[:1024],
                                inline=False,
                            )
                    for horizon in (5, 15):
                        stats = report["last_7d"][horizon]
                        earlier = report["prior_7d"][horizon]
                        n = stats["settled"]
                        scored = int(stats.get("scored", n) or 0)
                        if n == 0 or scored == 0:
                            value = (
                                f"Issued: **{stats['issued']}** · settled: **{n}**, "
                                f"valid scored pairs: **{scored}**. "
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
                                + (
                                    f"Brier loss: **{stats['brier']:.4f}** vs "
                                    f"baseline **{stats['baseline_brier']:.4f}** "
                                    "(lower is better)."
                                    if stats["brier"] is not None
                                    and stats["baseline_brier"] is not None
                                    else "Brier comparison unavailable: unscored settlements."
                                )
                            )
                            if scored != n:
                                value += (
                                    f"\nOnly {scored}/{n} settled records have "
                                    "valid paired scores. No approval inferred."
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
                        if (earlier["settled"] >= 50 and n >= 50
                            and earlier["brier"] is not None and stats["brier"] is not None
                            and earlier.get("scored", earlier["settled"]) == earlier["settled"]
                            and scored == n):
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
                    # Never replace verified out-of-sample results with a
                    # compelling-looking near-term price move. A short period
                    # of market collection is not a validated betting edge.
                    recent_5 = report["last_7d"][5]
                    recent_15 = report["last_7d"][15]
                    embed.add_field(
                        name="Tonight's research verdict",
                        value=(
                            "**NO VERIFIED BETTING EDGE.** RivaL may observe "
                            "live prices and issue shadow predictions, but "
                            f"only **{recent_5['settled']}** five-minute and "
                            f"**{recent_15['settled']}** fifteen-minute "
                            "prospective forecasts have settled in this "
                            "seven-day window. Real fill costs, funding, "
                            "slippage and liquidation risk are not fully "
                            "validated. No auto-betting orders are placed. "
                            "Do not interpret a green feed or historical "
                            "momentum as a recommended trade."
                        )[:1024], inline=False,
                    )
                    # Overview and horizon summaries already have their verified
                    # prospective results; advanced analytics belongs to its own section.
                    if self.section in ("overview", "five", "fifteen", "readiness", "venue"):
                        return embed
                    # Database evidence is queried at every Accuracy screen
                    # render, never a frozen Discord embed or cached AI claim.
                    try:
                        details = await asyncio.to_thread(
                            analytics_snapshot, self.market, asset=self.asset,
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("Advanced RivaL analytics unavailable")
                        embed.add_field(
                            name="Independent track analytics",
                            value="The forward analytics query is unavailable. "
                                  "No accuracy numbers were inferred.",
                            inline=False,
                        )
                    else:
                        for horizon in (5, 15):
                            item = details["math"][horizon]
                            recent = item["last_7d"]
                            gate = details["model_governance"].get(horizon)
                            brier = (
                                f"{recent['brier']:.4f}"
                                if recent["brier"] is not None else "pending"
                            )
                            calibration = (
                                f"{recent['ece']:.4f}"
                                if recent["ece"] is not None else "pending"
                            )
                            stress = item["cost_stress_7d"]
                            harsh = stress["scenarios"]["severe_stress"]
                            cost = (
                                f"{harsh['mean_net_bps']:+.1f} bps"
                                if harsh["mean_net_bps"] is not None
                                else "not measurable"
                            )
                            state = (
                                gate["status"].replace("_", " ")
                                if gate is not None else "no review yet"
                            )
                            embed.add_field(
                                name=f"Math {horizon}m · independent UTC windows",
                                value=(
                                    f"**{recent['events']}** forward events, "
                                    f"**{details['math_pending']}** unresolved "
                                    "per-exchange forecasts.\n"
                                    f"Brier: {brier} · calibration gap: {calibration}\n"
                                    f"Baseline-adjusted 7d skill trend: **{item['trend']}** "
                                    "(100+ unique events per period; "
                                    "observational research only).\n"
                                    f"Shadow model gate: **{state}**.\n"
                                    "Net after hypothetical severe "
                                    f"{harsh['assumed_total_bps']:.0f} bps "
                                    f"fees/slippage/adverse funding: **{cost}** "
                                    f"({stress['unique_decided_targets']} UTC targets, "
                                    f"{stress['utc_days']} days; "
                                    f"{harsh['status'].replace('_', ' ')}). "
                                    "NOT actual fills or trading approval."
                                )[:1024],
                                inline=False,
                            )
                        for horizon in (5, 15):
                            reliability = details["math"][horizon][
                                "forward_reliability_7d"
                            ]
                            ece = (
                                f"{reliability['ece']:.4f}"
                                if reliability["ece"] is not None
                                else "not measurable"
                            )
                            bins = [
                                f"{part['bin'] / 10:.1f}–"
                                f"{(part['bin'] + 1) / 10:.1f}: "
                                f"n={part['n']}, "
                                f"p={part['average_probability']:.2f}, "
                                f"observed={part['observed_up_rate']:.2f}"
                                for part in reliability["bins"][:5]
                            ]
                            embed.add_field(
                                name=f"Forward {horizon}m calibration",
                                value=(
                                    f"**{reliability['unique_targets']}** independent "
                                    f"UTC target outcomes over **{reliability['days']}** "
                                    f"days; conflicting venue labels: "
                                    f"**{reliability['disputed_targets']}**.\\n"
                                    f"Expected calibration error: **{ece}**. "
                                    f"Status: **{reliability['status'].replace('_', ' ')}**.\\n"
                                    f"Bin evidence (predicted vs observed):\\n"
                                    + ("\\n".join(bins) if bins else "Not enough outcomes.")
                                    + "\\nHistorical bins are descriptive, NOT "
                                      "a calibrated deployable model or trading approval."
                                ).replace("\\n", "\n")[:1024],
                                inline=False,
                            )
                        ai_status = details["gemini"]
                        quality = (
                            f"{ai_status['directional_accuracy'] * 100:.1f}%"
                            if ai_status["directional_accuracy"] is not None
                            else "not measurable"
                        )
                        embed.add_field(
                            name="Historical external AI · forward outcomes",
                            value=(
                                f"Issued **{ai_status['issued']}**, "
                                f"settled **{ai_status['settled']}**, "
                                f"directional decisions **{ai_status['decided']}**, "
                                f"uncertain/abstain **{ai_status['abstained']}**.\n"
                                f"Observed hit rate **{quality}**. "
                                "Self-reported confidence is NOT calibrated. "
                                "Source health and provider availability may "
                                "cause skipped cycles."
                            )[:1024],
                            inline=False,
                        )
                    embed.add_field(
                        name="How accuracy is audited",
                        value=(
                            "These are future-outcome scores, not historical "
                            "backfill wins. Each forecast is immutable and "
                            "settled once. Exchange returns are correlated, "
                            "so summed scores are NOT independent bets. "
                            "A better score on a small sample doesn't prove "
                            "a profitable edge after fees, funding or slippage. "
                            "Cost stress uses hypothetical 16/40/80 bps "
                            "round trips with fees, entry+exit slippage and "
                            "adverse funding allowances. One UTC target counts "
                            "once. 100 decisions over 7 days plus daily "
                            "consistency are required even for a hypothetical "
                            "pass. Real fills, funding payments, liquidation "
                            "and position sizing have NOT been verified."
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
                "The optional AI reviewer reads **all healthy exchanges**, "
                "for a **5-minute forecast**, with available 5m, 15m, "
                "1h, 4h and 24h returns and volatility "
                "features. Unavailable sources are excluded and disclosed."
            ), inline=False,
        )
        if self.ai is not None:
            try:
                state = await self.ai.request_status()
                configured = bool(state["ready"])
                embed.add_field(
                    name="Private LFM3 Intelligence",
                    value=(
                        f"Model: **{discord.utils.escape_markdown(state['model'])}**\n"
                        + (
                            "Configured for on-demand inference. **No paid tokens, "
                            "daily AI budget, cooldown, or external fallback.**\n"
                            "The inference service queues/limits CPU work. "
                            "Live host readiness is not checked on this screen."
                            if configured else
                            "Private RIVAL_LOCAL_AI_TOKEN is not configured. "
                            "AI research is unavailable; mathematical learning continues."
                        )
                    )[:1024],
                    inline=False,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("RivaL private Intelligence status unavailable")
                embed.add_field(
                    name="Intelligence status",
                    value="Local AI configuration could not be read. No inference requested.",
                    inline=False,
                )
        if self.opinion is not None and self.opinion.asset == self.asset:
            answer = self.opinion
            embed.add_field(
                name="AI research · UNCALIBRATED",
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
                        f"AI reviewer **{self.ai.model}** is configured for "
                        "on-demand, cached analysis."
                        if self.ai is not None and self.ai.available else
                        "No AI reviewer key is visible to this RivaL process."
                    )
                ), inline=False,
            )
        embed.set_footer(
            text="New candles invalidate cached evidence • Daily DB quota • Not financial advice"
        )
        return embed

    def _display_generation(self) -> ResearchDashboard:
        """Build new buttons without detaching the controls currently on Discord."""
        fresh = ResearchDashboard(
            owner_id=self.owner_id, market=self.market, ai=self.ai,
            venues=self.venues, shadow_enabled=self.shadow_enabled,
            target_service=self.target_service,
        )
        fresh.asset = self.asset
        fresh.screen = self.screen
        fresh.section = self.section
        fresh.selected_venue = self.selected_venue
        fresh.opinion = self.opinion
        fresh.message = self.message
        fresh._history_cache = self._history_cache
        fresh._navigation_state = self._navigation_state
        fresh._rebuild()
        return fresh

    async def _finish_redraw(
        self, interaction: discord.Interaction, fresh: ResearchDashboard,
        revision: int,
    ) -> None:
        """Finish a deferred research read without blocking the button handler."""
        state = self._navigation_state
        try:
            embed = await asyncio.wait_for(fresh.render(), timeout=10)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _LOG.warning(
                "Research render timed out screen=%s section=%s",
                fresh.screen, fresh.section,
            )
            embed = discord.Embed(
                title="⏳ Results temporarily delayed",
                description=(
                    "This database check did not finish in time. "
                    "The previous results are not proof of a new trade. "
                    "Use Update live stats or choose another section."
                ),
                color=discord.Color.orange(),
            )
        except Exception:  # noqa: BLE001 - preserve usable controls on failures
            _LOG.exception("RivaL research render failed")
            embed = discord.Embed(
                title="Research temporarily unavailable",
                description="Could not read verified results. Try another section.",
                color=discord.Color.orange(),
            )
        if state["revision"] != revision:
            return
        try:
            await asyncio.wait_for(
                interaction.edit_original_response(embed=embed, view=fresh), timeout=5,
            )
        except (discord.HTTPException, TimeoutError):
            _LOG.warning(
                "Research edit failed screen=%s section=%s",
                fresh.screen, fresh.section,
            )

    async def redraw(
        self, interaction: discord.Interaction, *, acknowledged: bool = False,
    ) -> None:
        # Discord needs acknowledgment before any database or model queries.
        if not acknowledged:
            await interaction.response.defer()
        if not isinstance(interaction, discord.Interaction):
            # Lightweight test interactions still exercise the entire renderer.
            self._rebuild()
            await interaction.edit_original_response(embed=await self.render(), view=self)
            return
        state = self._navigation_state
        state["revision"] = int(state["revision"]) + 1
        revision = int(state["revision"])
        prior = state["pending"]
        if isinstance(prior, asyncio.Task) and not prior.done():
            prior.cancel()
        fresh = self._display_generation()
        loading = discord.Embed(
            title=f"🔄 {self.asset} | {dict(_SECTIONS[self.screen])[self.section]}",
            description="Checking saved market data and verified outcomes. No trade is approved.",
            color=discord.Color.blurple(),
        )
        try:
            await asyncio.wait_for(
                interaction.edit_original_response(embed=loading, view=fresh), timeout=5,
            )
        except (discord.HTTPException, TimeoutError):
            _LOG.warning("Research loading display failed; old controls remain usable")
            return
        if state["revision"] != revision:
            return
        task = asyncio.create_task(
            self._finish_redraw(interaction, fresh, revision),
            name="rival-research-detail",
        )
        state["pending"] = task

