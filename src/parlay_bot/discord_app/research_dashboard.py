"""RivaL's private dual-track research menu. No listicle of ten feeds."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.answer_cards import (
    RESEARCH_FOOTER,
    AnswerCard,
    boosted_card,
    costs_card,
    engine_card,
    lineage_card,
    polish_embed,
    proof_card,
    readiness_card,
)
from parlay_bot.discord_app.home import fit_embed_field_value, rate_or_too_few
from parlay_bot.discord_app.opportunities import crypto_desk_embed
from parlay_bot.discord_app.patient_render import patient, still_loading
from parlay_bot.perpetuals.ai_types import AiResearch, ResearchUnavailable
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
from parlay_bot.perpetuals.history_inventory import history_inventory
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.local_first_research import (
    EVIDENCE_MODEL,
    LOCAL_MODEL,
    LocalFirstResearch,
)
from parlay_bot.perpetuals.market_pulse import calculate_market_pulse
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.polymarket_history import (
    fairness_text,
    polymarket_history,
    results_text,
)
from parlay_bot.perpetuals.polymarket_tracking import (
    PolymarketTracking,
    polymarket_tracking_text,
)
from parlay_bot.perpetuals.proof_progress import proof_progress, proof_progress_text
from parlay_bot.perpetuals.readiness import explain_venue_readiness, forecast_readiness
from parlay_bot.perpetuals.shadow_monitor import MIN_EVALUATED, ShadowLearningMonitor
from parlay_bot.perpetuals.spike_watch import SpikeWatch
from parlay_bot.research.source_native import VENUES, source_month_readiness
from parlay_bot.storage.optional_reads import optional_rows

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
        ("overview", "Polymarket results"),
        ("fair", "Is the price fair?"),
        ("pulse", "Price momentum now"),
        ("spikes", "Spikes & volume"),
        ("learning", "Is RivaL learning?"),
        ("history", "Data health (technical)"),
        ("recovery", "Backfill progress (technical)"),
        ("ai", "AI Review"),
    ),
    # Cleanup (Nov 11): Bot Performance is the summary; Results holds the
    # deep dives. "At a glance" repeated Performance and is gone (an old
    # link opens Models on trial); the other two summary cards are now
    # labelled as the detail sections they are.
    "accuracy": (
        ("better", "Models on trial"),
        ("money", "Would it make money?"),
        ("accuracy", "Accuracy details"),
        ("running", "Running details"),
        ("technical", "Technical evidence"),
    ),
    "ai": (
        ("overview", "Overview"),
        ("limits", "Intelligence status"),
        ("analysis", "AI assessment"),
    ),
}
# Audit Step 3 (2026-11-10): the accuracy ("Results") screen lists 5
# consolidated sections + the raw technical page, not 14. NOTHING is
# deleted: each old section survives as a detail topic inside its new
# home, reachable behind a detail button on the consolidated card.
# Old keys still resolve — a stale menu or deep link lands on the
# consolidated answer, never a dead section.
ACCURACY_SECTIONS = {
    "overview": ("overview",),
    "running": ("engine", "readiness", "venue"),
    "accuracy": ("five", "fifteen", "calibration", "ledger"),
    "better": ("progress", "lineage", "tracker"),
    "money": ("costs", "method"),
    "technical": ("technical",),
}
LEGACY_SECTION_MAP = {
    old: new for new, olds in ACCURACY_SECTIONS.items() for old in olds
}
# "At a glance" repeated Bot Performance: an old link or menu opens the
# first Results section instead (cleanup, Nov 11).
LEGACY_SECTION_MAP["overview"] = "better"
# The old section labels, reused for detail buttons and the compact
# description when a detail topic is open.
ACCURACY_DETAIL_LABELS = {
    "engine": "Is the engine running?",
    "readiness": "Can RivaL predict yet?",
    "venue": "Which exchange is blocked?",
    "five": "5-minute predictions",
    "fifteen": "15-minute predictions",
    "calibration": "Are probabilities reliable?",
    "ledger": "Recent predictions",
    "progress": "Is any model proven better?",
    "lineage": "How is RivaL learning?",
    "tracker": "Track next predictions",
    "costs": "Profit after trading fees",
    "method": "How results are checked",
}


def _resolve_section(screen: str, section: str) -> str:
    """An old accuracy key resolves to its consolidated card."""
    if screen == "accuracy":
        return LEGACY_SECTION_MAP.get(section, section)
    return section


# Audit Step 2: field names use the one display-name set (answer_cards).
# Each tuple lists the new name first, then the old name, so the compact
# view still finds a field built by older code paths or tests.
_FIELD_GROUPS = {
    "math": {
        "ai": ("AI research · UNCALIBRATED", "Analysis status",
               "Private LFM3 Intelligence", "Intelligence status"),
        "fair": ("Is the Polymarket price fair?",),
        "pulse": ("Current market pulse",),
        "spikes": ("Spike watch analytics", "Recent spike events"),
        "history": ("Source-native training readiness", "Historical gap audit"),
        "recovery": ("Historical recovery progress",),
        "learning": ("Main model", "Mathematical model",
                     "Automatic test model evaluation",
                     "Automatic shadow evaluation", "Automatic learning"),
    },
    "accuracy": {
        "engine": ("Automatic learning engine", "Forward issue audit"),
        "readiness": ("Main model forecast readiness",
                      "Mathematical forecast readiness",),
        "progress": ("Future model proof", "Prospective skill by market",
                     "Prospective skill by market regime",
                     "Test-model detail (5-minute)", "Trial detail (5-minute)"),
        "tracker": ("Prediction tracking",),
        "lineage": ("How is RivaL learning?", "Are boosted history models ready?",
                    "Are advanced models ready?"),
        "technical": ("Model training actually used",
                      "Boosted history model production proof",
                      "Boosted model production proof"),
        "venue": ("Why this exchange",),
        "five": ("Forward 5-minute evaluation", "Math 5m"),
        "fifteen": ("Forward 15-minute evaluation", "Math 15m"),
        "calibration": ("Forward 5m calibration", "Forward 15m calibration"),
        "ledger": ("Prediction ledger · prospective",),
        "costs": ("Execution cost verification", "How accuracy is audited"),
        "method": ("How accuracy is audited",),
        # Audit Step 3: the consolidated cards show the headline
        # answer-first card of each absorbed topic (live feed + two);
        # the rest of the topics sit behind detail buttons.
        "running": ("Automatic learning engine",
                    "Main model forecast readiness",
                    "Mathematical forecast readiness",
                    "Why this exchange", "Forward issue audit"),
        "accuracy": ("Forward 5-minute evaluation",
                     "Forward 15-minute evaluation", "Research verdict",
                     "Forward 5m calibration", "Forward 15m calibration",
                     "Prediction ledger · prospective"),
        "better": ("Future model proof",
                   "Prospective skill by market",
                   "Prospective skill by market regime",
                   "How is RivaL learning?",
                   "Are boosted history models ready?",
                   "Are advanced models ready?",
                   "Prediction tracking",
                   "Test-model detail (5-minute)",
                   "Trial detail (5-minute)"),
        "money": ("Execution cost verification", "How accuracy is audited"),
    },
    "ai": {
        "limits": ("Private LFM3 Intelligence", "Intelligence status"),
        "analysis": ("AI research · UNCALIBRATED", "Analysis status"),
    },
}



def _answered_by(model: str) -> str:
    """Which model actually produced the shown answer (not the configured one)."""
    if model == LOCAL_MODEL:
        return "RivaL Intelligence (LFM3 AI)"
    if model == EVIDENCE_MODEL:
        return "basic evidence summary (the AI was unavailable)"
    return discord.utils.escape_markdown(str(model))[:80]

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
        dashboard.detail = None
        dashboard.model = None
        dashboard.compare = []
        await dashboard.redraw(interaction)


class _DetailButton(discord.ui.Button["ResearchDashboard"]):
    """One absorbed topic behind a consolidated card (audit Step 3).

    Opens that topic's old section content — one sentence per number,
    per Step 2's rules — with a back button to the consolidated card.
    """

    def __init__(self, topic: str) -> None:
        super().__init__(
            label=ACCURACY_DETAIL_LABELS[topic],
            style=discord.ButtonStyle.secondary,
            custom_id=f"rival:research:detail:{topic}", row=2,
        )
        self.topic = topic

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.detail = self.topic
        await dashboard.redraw(interaction)


class _DetailBackButton(discord.ui.Button["ResearchDashboard"]):
    """Back from a detail topic to its consolidated card (audit Step 3)."""

    def __init__(self, section: str) -> None:
        super().__init__(
            label=f"← Back to {dict(_SECTIONS['accuracy'])[section]}",
            style=discord.ButtonStyle.secondary,
            custom_id="rival:research:detail:back", row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.detail = None
        await dashboard.redraw(interaction)


_VENUE_NAMES = {
    "bitget": "Bitget", "deribit": "Deribit", "dydx": "dYdX", "gate": "Gate",
    "hyperliquid": "Hyperliquid", "kucoin": "KuCoin", "lighter": "Lighter",
    "okx": "OKX", "phemex": "Phemex",
}


def _recovery_line(row: dict, *, now: datetime | None = None,
                   missing_bars: int | None = None) -> str:
    """One plain backfill status per exchange, without internal codes.

    ``missing_bars`` is the latest gap-audit count for this venue when one
    exists (``None`` when no audit has completed yet). A venue only renders
    as done when its recovery status says caught up AND the audit knows of
    no missing bars. A recently-failed venue never renders as done, even
    when its cursor happens to be recent.
    """
    name = _VENUE_NAMES.get(str(row["venue"]), str(row["venue"]))
    clock = now or datetime.now(UTC)
    imported = int(row.get("inserted_bars") or 0)
    status = str(row.get("status") or "")
    try:
        cursor = datetime.fromisoformat(str(row["next_opened_at"]))
        start = datetime.fromisoformat(str(row["started_from"]))
    except (KeyError, TypeError, ValueError):
        return f"**{name}**: {imported:,} old candles saved"
    backward = start > cursor  # OKX walks back from where it started
    if status == "pending" and not imported:
        return f"**{name}**: not started yet"
    # Failure states keep their own wording below; the 6h-cursor shortcut
    # must never promote one to done.
    failed = status in (
        "blocked", "retry", "source_recent_limit", "upstream_empty",
    )
    known_gaps = missing_bars is not None and int(missing_bars) > 0
    if status == "caught_up" and not known_gaps:
        state = "✅ done"
    elif status == "caught_up":
        # The gap audit found missing bars the cursor alone cannot see.
        state = f"⏳ caught up, incomplete — {int(missing_bars):,} missing bars"
    elif status == "gaps_pending":
        state = "⏳ caught up, incomplete — re-checking a few gaps"
    elif status == "gap_repair":
        state = "⏳ repairing gaps"
    elif not failed and not backward and clock - cursor <= timedelta(hours=6):
        state = "✅ done"
    elif status == "source_recent_limit":
        state = "⛔ this exchange's API only serves recent candles"
    elif status == "upstream_empty":
        state = "⛔ the exchange returned no older candles"
    elif status == "blocked":
        state = "⛔ blocked by the exchange (will retry)"
    elif status == "retry":
        state = "⚠️ last attempt failed, retrying"
    elif backward:
        state = f"⏳ filled back to {cursor:%b %d}"
    else:
        state = f"⏳ filled up to {cursor:%b %d}"
    return f"**{name}**: {state} · {imported:,} old candles saved"


def _when(value) -> str:
    """A stored ISO timestamp as a Discord relative time, never raw ISO text."""
    if not value:
        return "not yet"
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return discord.utils.format_dt(stamp, style="R")


def _short(value: str, limit: int = 225) -> str:
    """Trim verbose boilerplate ONLY in summaries; details retain full text."""
    compact = " ".join(value.replace("\\n", "\n").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit - 1].rsplit(" ", 1)[0] + "…"


def _strip_raw(value: str) -> str:
    """The answer, reason, next step and details, without the "Raw numbers:"
    block that repeats them in technical words (Nov 11 audit: every card
    said each number twice). The raw block stays one tap away, on the
    topic's own detail view."""
    from parlay_bot.discord_app.answer_cards import RAW_MARKER

    head, sep, _raw = value.partition(RAW_MARKER)
    return head.rstrip() if sep and head.strip() else value


def _compact_embed(full: discord.Embed, *, screen: str, section: str,
                   raw: bool = True) -> discord.Embed:
    """A real phone-sized screen; detailed metrics are separate screens."""
    # Audit Step 3: a detail topic keeps its old label in the header.
    label = dict(_SECTIONS[screen]).get(section)
    if label is None:
        label = (
            ACCURACY_DETAIL_LABELS.get(section, "Overview")
            if screen == "accuracy" else "Overview"
        )
    result = discord.Embed(
        title=full.title,
        description={
            "math": "What really happened on past contracts. Not a prediction or a trade signal.",
            "accuracy": "Predictions checked against what really happened. Profit is not yet proven.",
            "ai": "Read-only AI research on source-verified market data.",
        }[screen] + f"\n**{label}** · Choose a section below for details.",
        color=full.color,
    )
    result.timestamp = full.timestamp
    fields = list(full.fields)

    # Live health is always visible and never served from the history cache.
    # Audit Step 2: the full render names this field "Live market feeds";
    # the old name is still accepted so older embeds keep working.
    source = next(
        (f for f in fields
         if "Live market feeds" in f.name or "Live collection status" in f.name),
        None,
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
            value = field.value if raw else _strip_raw(field.value)
            result.add_field(
                name=field.name,
                value=_short(value, 230) if short else value,
                inline=False,
            )
            return True
        return False

    if section == "overview":
        if screen == "math":
            add_one(("Polymarket results",))
            add_one(("Current market pulse",), short=True)
        elif screen == "accuracy":
            add_one(("Research verdict", "Accuracy tracking"), short=True)
            add_one(("Forward 5-minute evaluation",), short=True)
        else:
            add_one(("AI research · UNCALIBRATED", "Analysis status"), short=True)
            add_one(("AI provider & request limits",), short=True)
    else:
        # Progress carries three short fields by design (proof checks,
        # regime splits, trial detail), so its compact cap is one
        # higher; every other section stays at live + two detailed
        # metrics.
        # Accuracy also keeps the no-edge verdict (it was on "At a glance",
        # which the Nov 11 cleanup removed), shortened.
        cap = 4 if section in ("progress", "accuracy") else 3
        for prefix in _FIELD_GROUPS[screen].get(section, ()):
            if len(result.fields) >= cap:
                break
            add_one((prefix,), short=prefix == "Research verdict")
    if len(result.fields) == (1 if source is not None else 0):
        result.add_field(
            name="No results yet",
            value="No verified records for this section yet. "
                  "The collector continues independently.",
            inline=False,
        )
    result.set_footer(text=RESEARCH_FOOTER)
    return result



def _plain_training_line(proof: dict, horizon: int) -> str:
    """One plain line per timeframe; training activity is not a prediction win."""
    label = f"**{horizon}-min:**"
    if not proof["forecasts"]:
        return f"{label} No recorded predictions to check yet (last 7 days)."
    if not proof["latest_lineage_verified"]:
        return (f"{label} Cannot confirm how the latest prediction was trained "
                f"(no training record on {proof['lineage_unknown']} of "
                f"{proof['forecasts']} recent predictions).")
    seeded, proven = int(proof["historically_seeded"]), int(proof["lineage_proven"])
    source = ("started from a verified historical model, then live data"
              if proof["latest_historical_seed_sha256"] else "live data only")
    return (
        f"{label} {source} · {_updates_text(proof)} · "
        f"history used in {seeded} of the last {proven} predictions."
    )


def _updates_text(proof: dict) -> str:
    """Per-exchange live update counts as a range (each venue learns separately)."""
    low, high = proof.get("online_training_updates_low"), proof.get("online_training_updates_high")
    venues = int(proof.get("online_training_venues") or 0)
    if low is not None and high is not None and venues > 1 and low != high:
        return f"{int(low):,}–{int(high):,} live updates per exchange ({venues} exchanges)"
    if low is not None and venues >= 1:
        return f"{int(low):,} live updates so far"
    return f"{int(proof['latest_online_training_updates']):,} live updates so far"


def _history_headline(proofs: list[dict]) -> str:
    seen = [p for p in proofs if p.get("lineage_proven")]
    if not seen:
        return "**Is it learning from history?** Cannot tell yet."
    seeded = sum(int(p["historically_seeded"]) for p in seen)
    if not seeded:
        return ("**How is it learning? Live.** The main model updates itself after every "
                "graded result. It does not start from stored history; separate test "
                "models try that, and none has passed yet.")
    total = sum(int(p["lineage_proven"]) for p in seen)
    return (f"**Is it learning from history? Partly:** {seeded} of the last {total} "
            "predictions started from a verified historical model.")


def _plain_boosted_proof(proof: dict, *, incumbent: dict | None = None) -> str:
    """Distinguish rejected advanced models from the active model's forecasts."""
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
    incumbent_line = "Main model: see Results for separate forward records."
    if incumbent is not None:
        recent = incumbent.get("last_7d")
        if isinstance(recent, dict):
            try:
                rows = [recent[h] for h in (5, 15, 60)]
                main_issued = sum(int(row["issued"]) for row in rows)
                main_settled = sum(int(row["settled"]) for row in rows)
                coin = f"{incumbent['asset']}, " if incumbent.get("asset") else ""
                firsts = [row.get("first_issued") for row in rows if row.get("first_issued")]
                since = ""
                if firsts:
                    first = datetime.fromisoformat(str(min(firsts)))
                    if first.tzinfo is not None and datetime.now(UTC) - first < timedelta(days=6, hours=12):
                        since = f", live since {first:%b %d}"
                if min(main_issued, main_settled) >= 0 and main_settled <= main_issued:
                    incumbent_line = (
                        f"**Main model ({coin}all timeframes, 7d{since}):** "
                        f"{main_issued:,} predictions, {main_settled:,} graded."
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
    return (
        f"**Advanced models built from history so far:** {int(proof['portable_imported']):,} "
        "(every training run and timeframe counted). "
        f"**Passed screening:** {int(proof['portable_screened']):,}.\n"
        f"**What's holding them back:** {failures}.\n"
        f"**Advanced-model forecasts (30d):** {int(proof['issued']):,} logged; "
        f"{int(proof['settled']):,} settled; "
        f"{int(proof['quote_exits']):,} matched exit quotes.\n"
        f"{incumbent_line}\n"
        f"**Ready for practice-only tests:** {paper}. {verdict}\n"
        "Real profit after fees, funding and slippage is unverified."
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
        dashboard.model = None
        dashboard.compare = []
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
        polymarket = dashboard.track_platform == "polymarket"
        if dashboard.market is None or (not polymarket and not dashboard.selected_venue):
            dashboard.message = "No saved exchange feed available for tracking."
        elif polymarket:
            horizon, target = map(int, self.values[0].split(":"))
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        PolymarketTracking(dashboard.market.store).start,
                        owner_id=dashboard.owner_id, asset=dashboard.asset,
                        horizon=horizon, target=target,
                    ),
                    timeout=8,
                )
                dashboard.message = (
                    f"Tracking RivaL's next {result['target']} Polymarket "
                    f"{dashboard.asset} {horizon}m calls."
                    if result["status"] == "started" else
                    "A Polymarket tracking session for this coin and timeframe is "
                    "still running. It will not be reset."
                )
            except TrackingUnavailable as exc:
                dashboard.message = str(exc)
            except Exception:  # noqa: BLE001 - preserve original Discord controls
                _LOG.exception("Could not start RivaL Polymarket tracking session")
                dashboard.message = (
                    "Could not record this session. No past calls were counted."
                )
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


class _TrackPlatformButton(discord.ui.Button["ResearchDashboard"]):
    """Which platform's predictions to follow: Polymarket or exchange futures."""

    def __init__(self, platform: str):
        polymarket = platform == "polymarket"
        super().__init__(
            label=("Platform: Polymarket" if polymarket else "Platform: exchange futures"),
            emoji="🎰" if polymarket else "📈",
            custom_id="rival:research:track-platform",
            style=discord.ButtonStyle.success if polymarket else discord.ButtonStyle.secondary,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.track_platform = (
            "exchange" if dashboard.track_platform == "polymarket" else "polymarket"
        )
        dashboard.message = None
        await dashboard.redraw(interaction)


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
        # The venue picker lives on the proof detail topic now.
        dashboard.section = "better"
        dashboard.detail = "progress"
        await dashboard.redraw(interaction)


class _ModelSelect(discord.ui.Select["ResearchDashboard"]):
    """Pick any model on trial for this coin to open its own detail page."""

    def __init__(self, names: list[str], selected: list[str]) -> None:
        from parlay_bot.perpetuals.model_lab import family_names, family_settings

        labels = family_names([name for name in names if name.startswith("lab_")])
        options = [discord.SelectOption(
            label=labels.get(name, name),
            value=name, default=name in selected,
            description=(name.split("_")[1] + " · " + (family_settings(name) or "Design unavailable"))[:100]
                        if name.startswith("lab_") else None)
                   for name in names[:25]]
        super().__init__(
            placeholder="🧪 Inspect one model, or pick up to 5 to compare…",
            custom_id="rival:research:model", row=4, options=options,
            min_values=1, max_values=min(5, len(options)),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        picked = list(self.values)
        # One model: its own page. Two or more: side by side.
        dashboard.model = picked[0] if len(picked) == 1 else None
        dashboard.compare = picked if len(picked) > 1 else []
        dashboard.detail = None
        await dashboard.redraw(interaction)


class _WatchLiveButton(discord.ui.Button["ResearchDashboard"]):
    """Post the open model page or comparison as a self-updating watch."""

    def __init__(self) -> None:
        super().__init__(label="Watch live", emoji="📌",
                         style=discord.ButtonStyle.success,
                         custom_id="rival:research:watch", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        names = dashboard.compare or ([dashboard.model] if dashboard.model else [])
        service = dashboard.watch_service
        if service is None or not names:
            await interaction.response.send_message(
                "Live watching isn't available right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        notice = await service.start(interaction, dashboard.asset, names)
        await interaction.followup.send(notice, ephemeral=True)


class _ModelBackButton(discord.ui.Button["ResearchDashboard"]):
    def __init__(self) -> None:
        super().__init__(label="← Back to Models on trial",
                         style=discord.ButtonStyle.secondary,
                         custom_id="rival:research:model:back", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        dashboard = self.view
        assert dashboard is not None
        dashboard.model = None
        dashboard.compare = []
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
        dashboard.detail = None
        dashboard.model = None
        dashboard.compare = []
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
            ok, message = False, "Could not start monitoring. Try /rival → Advanced → Manual Price Target."
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
        dashboard.detail = None
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


def _forward_eval_card(horizon: int, stats: dict, scored: int, raw: str) -> str:
    """Answer-first wrap for one Forward N-minute evaluation field.

    The verdict comes from the same numbers the field already shows:
    ``model_beats_baseline`` with 50+ graded results is a Yes/No, fewer
    than 20 graded results is Not yet, nothing graded is Can't tell.
    A Yes here is "beats the coin flip so far" — never proof of an
    edge; the after-cost proof is a separate card. The full field text
    stays under "Raw numbers:".
    """
    n = int(stats.get("settled") or 0)
    issued = int(stats.get("issued") or 0)
    venues = int(stats.get("venues") or 0)
    brier, baseline = stats.get("brier"), stats.get("baseline_brier")
    beats = stats.get("model_beats_baseline")
    details = [f"Issued {issued:,}, graded {n:,} across {venues} exchanges."]
    hit = stats.get("hit_rate")
    if isinstance(hit, (int, float)) and scored >= 20:
        details.append(
            f"Called the direction right {hit * 100:.1f}% of {scored:,} graded calls.")
    if isinstance(brier, (int, float)) and isinstance(baseline, (int, float)):
        reason = f"Error score {brier:.4f} vs coin flip {baseline:.4f}."
    else:
        reason = "Error score comparison unavailable."
    if n == 0 or scored == 0:
        verdict, answer = "Can't tell", "Can't tell — no graded predictions yet."
        reason = "No predictions have been graded yet."
        next_step = "Needs 50 graded results to judge."
    elif scored < 20:
        verdict, answer = "Not yet", "Not yet — too few graded results to judge."
        reason = f"Only {scored} graded results so far."
        next_step = f"Needs 50 graded results; has {scored}."
    elif beats is True:
        verdict, answer = "Yes", "Yes — beats the coin flip so far."
        next_step = "Next: check the after-cost proof before trusting it."
    elif beats is False:
        verdict, answer = "No", "No — not beating the coin flip yet."
        next_step = (f"Its error is higher than a coin flip's over {scored:,} graded "
                     "results; it has to get lower to count.")
    else:
        verdict, answer = "Not yet", "Not yet — skill not judged in this window."
        next_step = f"Needs 50 graded results; has {scored}."
    return AnswerCard(
        question=f"Forward {horizon}-minute evaluation",
        verdict=verdict, answer=answer, reason=reason,
        next_step=next_step, details=tuple(details), raw=raw,
    ).field_value()


def _calibration_card(horizon: int, reliability: dict, raw: str) -> str:
    """Answer-first wrap for one Forward N-minute calibration field.

    No calibration status is a pass — even the best is a preliminary
    screen only — so this card never answers Yes. The bin table stays
    under "Raw numbers:".
    """
    targets = int(reliability.get("unique_targets") or 0)
    days = int(reliability.get("days") or 0)
    ece = reliability.get("ece")
    status = str(reliability.get("status") or "")
    reason = (
        f"Expected calibration error: {ece:.4f}."
        if isinstance(ece, (int, float)) else
        "Expected calibration error: not measurable."
    )
    details = (
        f"{targets} independent targets over {days} days.",
        f"Conflicting venue labels: {int(reliability.get('disputed_targets') or 0)}.",
    )
    if status == "insufficient_independent_forward_history" and raw.startswith("ℹ️ Not measured"):
        # Nov 11 audit: say WHY in plain words instead of "too few
        # pre-window predictions" (the raw block alone explained it).
        verdict, answer = "Can't tell", (
            f"Can't tell — {horizon}-min calls come 1–2 minutes into their window.")
        reason = ("That is fair for scoring (it only sees finished candles) but too late "
                  "to trade at the opening price, so this check skips them.")
        next_step = ("Bot Performance shows how often its stated chances came true "
                     "(\"Can you trust its confidence?\").")
    elif status == "insufficient_independent_forward_history":
        verdict, answer = "Not yet", (
            "Not yet — too few pre-window predictions to judge calibration.")
        next_step = "Needs 100 independent targets over 7 days."
    elif status == "calibration_gap_too_high":
        verdict, answer = "No", "No — probabilities are not reliable yet."
        next_step = "Needs a smaller calibration gap over 100 targets."
    elif status == "narrow_probability_coverage":
        verdict, answer = "Can't tell", (
            "Can't tell — probabilities cover too narrow a range.")
        next_step = "Needs predictions across more probability bins."
    else:  # preliminary_reliability_screen_only (or an unknown status)
        verdict, answer = "Not yet", (
            "Not yet — only a preliminary screen, not proof.")
        next_step = "Needs more targets before calling probabilities reliable."
    return AnswerCard(
        question=f"Forward {horizon}m calibration",
        verdict=verdict, answer=answer, reason=reason,
        next_step=next_step, details=details, raw=raw,
    ).field_value()


def _regime_line(family: str, horizon: int, proof: dict) -> str:
    """One family's per-regime gains for one horizon: three short numbers.

    Audit Step 2: the family label arrives already renamed
    ("main model", "trend learner", ...) and the regime words use the
    one display set (calm / normal / wild market); the proof dict keys
    stay the stored quiet/normal/turbulent.
    """
    display = {"quiet": "calm", "normal": "normal", "turbulent": "wild"}
    # Nov 11 audit: points (x1000, like every other "edge" in the bot) and
    # a plain verdict, instead of "+0.0157" and "regime instability detected".
    verdict = {
        "consistent_shadow_skill_research_only": "ahead in every market type",
        "insufficient_distinct_market_conditions": "not enough market types yet",
        "regime_instability_detected": "ahead in some market types, behind in others",
    }.get(proof["status"], proof["status"].replace("_", " "))
    gains = []
    for regime in ("quiet", "normal", "turbulent"):
        gain = proof["regimes"][regime]["paired_brier_gain"]
        gains.append(
            f"{display[regime]} {gain * 1000:+.1f}" if gain is not None
            else f"{display[regime]} —")
    return f"{family} {horizon}m · {verdict}: " + " · ".join(gains)


class ResearchDashboard(discord.ui.View):
    def __init__(
        self, *, owner_id: int, market: PerpetualMarketStore | None,
        ai: LocalFirstResearch | None,
        venues: tuple[str, ...],
        shadow_enabled: bool = False,
        target_service: DirectionTargetService | None = None,
        watch_service=None,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.market = market
        self.ai = ai
        self.venues = venues
        self.shadow_enabled = shadow_enabled
        self.target_service = target_service
        self.watch_service = watch_service
        # Models picked for the side-by-side card (two or more).
        self.compare: list[str] = []
        self.asset = "BTC"
        self.screen = "opportunities"
        self.section = "overview"
        # Audit Step 3: the open detail topic inside a consolidated
        # accuracy section (an old section key), else None.
        self.detail: str | None = None
        # The model on trial whose own page is open (Is anything better?).
        self.model: str | None = None
        # (kind, asset[, name]) -> (monotonic time, value); shared across
        # message generations like the history cache.
        self._model_cache: dict[tuple, tuple[float, object]] = {}
        self.selected_venue = venues[0] if venues else ""
        # Track Next defaults to Polymarket: that is where users place bets.
        self.track_platform = "polymarket"
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
                "Open your own private research panel with /rival-tools research.",
                ephemeral=True,
            )
            return False
        return True

    def _render_topics(self) -> tuple[str, ...]:
        """Old section keys rendered by this view: the open detail topic,
        else every topic the consolidated section absorbs. A detail
        topic from another section is ignored, never rendered stale."""
        if self.screen != "accuracy":
            return (self.section,)
        section = _resolve_section(self.screen, self.section)
        topics = ACCURACY_SECTIONS.get(section, (section,))
        if (self.detail is not None and self.detail in topics
                and self.detail != section):
            return (self.detail,)
        return topics

    def _rebuild(self) -> None:
        self.section = _resolve_section(self.screen, self.section)
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
        topics = self._render_topics()
        single = len(topics) == 1
        picker = self._model_picker()
        if picker and (self.model is not None or self.compare):
            self.add_item(_ModelBackButton())
            if self.watch_service is not None:
                self.add_item(_WatchLiveButton())
        elif self.screen == "accuracy" and not single:
            # One detail button per absorbed topic on the consolidated
            # card; the topic content itself is one tap away.
            for topic in topics:
                self.add_item(_DetailButton(topic))
        elif self.screen == "accuracy" and self.detail is not None:
            self.add_item(_DetailBackButton(self.section))
        if self.screen == "accuracy" and single and topics[0] == "tracker":
            self.add_item(_TrackStartSelect())
            self.add_item(_TrackPlatformButton(self.track_platform))
            if self.track_platform != "polymarket":
                self.add_item(_TrackVenueButton())
        if self.screen == "opportunities" and self.target_service is not None:
            self.add_item(_QuickTargetButton())
        if picker:
            # Row 4: the model picker replaces the exchange picker on the
            # "Is anything better?" card; the exchange picker stays on its
            # own detail topic, the one that uses it.
            names = self._cached_models()
            if names:
                picked = self.compare or ([self.model] if self.model else [])
                self.add_item(_ModelSelect(names, picked))
        elif (((self.screen == "accuracy"
                and bool(set(topics) & {"venue", "progress"}))
               or (self.screen == "math" and self.section == "history"))
                and self.venues):
            self.add_item(_VenueSelect(self.venues, self.selected_venue))
        if self.screen == "ai" or (self.screen == "math" and self.section == "ai"):
            enabled = self.market is not None and self.ai is not None and self.ai.available
            self.add_item(_AskButton(enabled))

    MODEL_CACHE_SECONDS = 600

    def _model_picker(self) -> bool:
        """The model picker belongs to the "Is anything better?" card."""
        return (self.screen == "accuracy" and self.section == "better"
                and (self.detail is None or self.model is not None or bool(self.compare))
                and self.market is not None)

    def _cached(self, key: tuple):
        hit = self._model_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.MODEL_CACHE_SECONDS:
            return hit[1]
        return None

    def _cached_models(self) -> list[str]:
        return list(self._cached(("names", self.asset)) or [])

    async def _load_models(self) -> None:
        """The trial list for this coin (one point read, cached 10 minutes)."""
        if self._cached(("names", self.asset)) is not None:
            return
        from parlay_bot.discord_app.model_detail import trial_models

        names = await asyncio.to_thread(trial_models, self.market.store, self.asset)
        self._model_cache[("names", self.asset)] = (time.monotonic(), names)
        self._rebuild()

    async def _model_page(self) -> discord.Embed:
        from parlay_bot.discord_app.model_detail import model_embed, model_report

        key = ("report", self.asset, self.model)
        report = self._cached(key)
        if report is None:
            report = await asyncio.to_thread(
                model_report, self.market.store, self.asset, self.model)
            self._model_cache[key] = (time.monotonic(), report or {})
        return model_embed(report or None, asset=self.asset, name=self.model)

    async def _compare_page(self) -> discord.Embed:
        from parlay_bot.discord_app.model_detail import model_report
        from parlay_bot.discord_app.model_watch import compare_embed

        reports = {}
        for name in self.compare:
            key = ("report", self.asset, name)
            report = self._cached(key)
            if report is None:
                report = await asyncio.to_thread(
                    model_report, self.market.store, self.asset, name)
                self._model_cache[key] = (time.monotonic(), report or {})
            reports[name] = report or None
        return compare_embed(reports, asset=self.asset)

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
        recovery = self._recovery_row("gate", "*")
        return audits, recovery

    # One backfill-progress read for every exchange card (was four copies).
    # Each read has its own connection via optional_rows: an older database
    # without the table shows "no progress", never breaks the screen.
    _PROGRESS_COLUMNS = "status,inserted_bars,next_opened_at,empty_windows,attempted_at"

    def _recovery_row(self, venue: str, columns: str | None = None) -> dict | None:
        if self.market is None:
            return None
        rows = optional_rows(
            self.market.store,
            f"SELECT {columns or self._PROGRESS_COLUMNS} FROM perp_history_recovery "
            "WHERE venue=? AND asset=?",
            (venue, self.asset), what="history recovery read") or []
        return dict(rows[0]) if rows else None

    def _kucoin_history_progress(self) -> dict | None:
        """Read KuCoin-specific recovery progress without fetching exchange data."""
        return self._recovery_row("kucoin")

    def _okx_history_progress(self) -> dict | None:
        """Reverse-chronological OKX six-month repair state from PostgreSQL."""
        return self._recovery_row("okx")

    def _bitget_history_progress(self) -> dict | None:
        """One DB query, no Bitget API request or stale embedded cache."""
        return self._recovery_row("bitget")

    def _all_history_progress(self) -> list[dict]:
        """Every exchange's backfill state for this coin (one DB query)."""
        if self.market is None:
            return []
        rows = optional_rows(
            self.market.store,
            """SELECT venue,status,inserted_bars,started_from,next_opened_at,
                      empty_windows,attempted_at
               FROM perp_history_recovery WHERE asset=? ORDER BY venue""",
            (self.asset,), what="history recovery read") or []
        return [dict(row) for row in rows]

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

    def _trial_detail_value(self) -> str:
        """The numbers the Performance-page headlines cut (5-minute).

        One line per trial family with the detail the headline no
        longer shows: Brier vs the simple guess, the vs-regime_v1
        head-to-head, the selection decomposition, coverage,
        availability, trending labels learned, weight drift, and the
        7d vs 8-30d gain split. Only the metrics a family actually
        carries are shown; a family with no scored results has no
        line. This is the one place the cut metrics live — the
        Performance page is headline-only now. Never raises: a read
        failure returns a short fallback, never hides the sections
        above it.
        """
        try:
            from parlay_bot.discord_app.home import (
                _pooled_record,
                _trial_detail_line,
            )
            from parlay_bot.perpetuals.regime_live_model import (
                BASE_FAMILY,
                FAST_FAMILY,
            )
            from parlay_bot.perpetuals.regime_live_model import (
                trial_report as live_trial_report,
            )
            from parlay_bot.perpetuals.regime_model import (
                trial_report as regime_trial_report,
            )
            from parlay_bot.perpetuals.wild_model import (
                trial_report as wild_trial_report,
            )

            store = self.market.store
            records = [
                ("6-year history model", _pooled_record(self.market, self.asset, 5)),
                ("trend-only test", regime_trial_report(store, asset=self.asset, horizon=5)),
                ("trend learner", live_trial_report(
                    store, asset=self.asset, horizon=5, family=BASE_FAMILY)),
                ("fast trend learner", live_trial_report(
                    store, asset=self.asset, horizon=5, family=FAST_FAMILY)),
                ("wild-only test", wild_trial_report(store, asset=self.asset, horizon=5)),
            ]
            lines = []
            for label, record in records:
                line = _trial_detail_line(label, record)
                if line:
                    lines.append(line)
            if not lines:
                return "No scored trial results yet — nothing to detail."
            return fit_embed_field_value("\n".join(lines))
        except Exception:  # noqa: BLE001 - detail is additive only
            _LOG.exception("RivaL trial detail read unavailable")
            return "Trial detail unavailable. No improvement claimed."

    async def _regime_field_value(self) -> str:
        """Paired Brier gain by market regime, one short line per family.

        One line per family per horizon (incumbent first, then the
        live families' issued subsets) with only the three per-regime
        gains — no target/day counts, no disputed counts. The counts
        are not rendered anywhere now; the gains are the evidence this
        field exists to show. regime_v2 is dead — never issued once,
        flag off — so it has no subset here. regime_v1 has no subset
        line either: its per-regime gain is +0.0000 by design (it
        returns the incumbent's own probability verbatim, so its
        model Brier equals the incumbent Brier on its issued subset),
        and a line that can never show anything else is not evidence.
        regime_v1 still appears in the Trial detail field and on the
        Performance-page trial rows, which report its honest trial
        result. The value is fitted with fit_embed_field_value: tail
        lines drop before any mid-word cut (the old [:1024] slice cut
        the disclaimer mid-word on real numbers). A failure in any
        subset never hides the sections above it.
        """
        try:
            monitor = ForwardAccuracyMonitor(self.market)
            lines = [("Edge by market type, in points (+ is better). Main model: vs a coin "
                      "flip. Test models: vs the main model on the calls they made.")]
            for horizon in (5, 15):
                proof = await asyncio.to_thread(
                    monitor.regime_summary,
                    asset=self.asset, horizon_minutes=horizon,
                )
                lines.append(_regime_line("main model", horizon, proof))
            # Family subsets: the same per-regime split, but only over
            # the targets each family actually issued on, so the page
            # shows whether the family won on the targets it chose.
            # regime_v1 is not a subset here: its gain is +0.0000 by
            # design (see the docstring), so its line can never show
            # anything else. A failure here never hides the sections
            # above.
            for subset_family, label in (
                    ("regime_live_v1", "trend learner"),
                    ("regime_live_fast_v1", "fast trend learner")):
                try:
                    for horizon in (5, 15):
                        proof = await asyncio.to_thread(
                            monitor.regime_summary,
                            asset=self.asset, horizon_minutes=horizon,
                            family=subset_family,
                        )
                        lines.append(_regime_line(label, horizon, proof))
                except Exception:  # noqa: BLE001 - subset is additive only
                    _LOG.exception("RivaL %s subset query unavailable", subset_family)
            return fit_embed_field_value(
                "\n".join(lines)
                + "\nRegimes were stamped BEFORE outcomes. "
                  "Multiple venues count once per target. "
                  "A stable shadow score is not trading profit."
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL regime-evidence query unavailable")
            return "Regime proof unavailable. No improvement claimed."

    async def render(self) -> discord.Embed:
        if self.screen == "opportunities":
            return await asyncio.to_thread(
                crypto_desk_embed, self.market, asset=self.asset,
                proof=self.section == "proof",
            )
        # Audit Step 3: an old accuracy key lands on its consolidated
        # card; a detail topic compacts by its own field groups.
        requested = self.section
        self.section = _resolve_section(self.screen, self.section)
        if self._model_picker():
            try:
                await self._load_models()
            except Exception:  # noqa: BLE001 - the picker is optional, the card is not
                _LOG.warning("Research model list unavailable", exc_info=True)
            if self.compare:
                return await self._compare_page()
            if self.model is not None:
                return await self._model_page()
        detailed = await self._render_full()
        topics = self._render_topics()
        compact_section = topics[0] if len(topics) == 1 else self.section
        # A consolidated card shows each answer once; a detail view (one
        # topic, opened by its button) and Technical keep the raw numbers.
        return _compact_embed(
            detailed, screen=self.screen, section=compact_section,
            raw=(self.screen != "accuracy" or self.detail is not None
                 or requested != self.section or self.section == "technical"),
        )

    async def _render_full(self) -> discord.Embed:
        """The full render, polished (audit Step 2).

        The inner render still builds every field; this wrapper applies
        the shared answer-card polish — the one name set, the single
        footer, and the feed-status rule — so no return path in the
        long inner function can forget it.
        """
        embed = await self._render_full_inner()
        return polish_embed(embed, section=self.section)

    async def _render_full_inner(self) -> discord.Embed:
        """Current authoritative data, never returned as a giant Discord embed."""
        # Audit Step 3: an old accuracy key lands on its consolidated
        # card; ``topics`` below is the one topic (detail view) or the
        # whole absorbed set (consolidated view).
        self.section = _resolve_section(self.screen, self.section)
        topics = self._render_topics()
        single = len(topics) == 1
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
                    f"**Checked:** {stamp} · **newest 5-min candle:** {bar}"
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

        if self.screen == "math" and self.section in ("overview", "fair"):
            if self.market is None:
                embed.add_field(name="Polymarket results",
                                value="Database unavailable.", inline=False)
            else:
                try:
                    history = await asyncio.to_thread(
                        polymarket_history, self.market.store, asset=self.asset,
                    )
                    if self.section == "overview":
                        embed.add_field(name=f"Polymarket results · {self.asset} Up/Down",
                                        value=results_text(history), inline=False)
                    else:
                        embed.add_field(name=f"Is the Polymarket price fair? · {self.asset}",
                                        value=fairness_text(history), inline=False)
                except Exception:  # noqa: BLE001 - never invent results
                    _LOG.exception("RivaL Polymarket history unavailable")
                    embed.add_field(name="Polymarket results",
                                    value="Settled Polymarket results could not be read.",
                                    inline=False)
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
                        value=fit_embed_field_value(AnswerCard(
                            question="Data health (technical)",
                            verdict=(
                                "Yes" if state["months_with_receipts"] >= 6
                                else "Not yet"
                            ),
                            answer=(
                                "Yes — all 6 months verified."
                                if state["months_with_receipts"] >= 6 else
                                f"Not yet — {state['months_with_receipts']} "
                                "of 6 months verified."
                            ),
                            reason=(
                                f"{state['venue']} / {state['contract']} "
                                f"({state['quote']})."
                            ),
                            next_step=(
                                "Needs 6 published source-month receipts."
                            ),
                            raw=(
                                f"**{state['venue']} / {state['contract']} ({state['quote']})**\n"
                                f"Published source-month receipts: "
                                f"**{state['months_with_receipts']}/6**.\n"
                                f"{observations}\n"
                                + ("Receipt table unavailable; status is unknown. "
                                   if not state["receipt_read_available"] else "")
                                + "Count-only months are NOT certified. "
                                  "Training still requires private Drive SHA-256 "
                                  "restoration; publication and future model proof are separate."
                            ),
                        ).field_value()),
                        inline=False,
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
            if self.section in ("history", "recovery"):
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
                                        value=fit_embed_field_value(
                                            f"**{kucoin['status']}** · "
                                            f"**{int(kucoin['inserted_bars']):,}** old "
                                            f"candles imported for {self.asset}. "
                                            f"Cursor: {str(kucoin['next_opened_at'])[:16]} UTC. "
                                            f"Missing intervals: **{int(kucoin['empty_windows'])}** "
                                            "empty request windows. "
                                            "These are NOT live forward tests."
                                        ), inline=False,
                                    )
                                del bitget  # every venue is listed below
                                rows = await asyncio.to_thread(self._all_history_progress)
                                seen = {str(row["venue"]) for row in rows}
                                missing_by_venue = {
                                    str(audit["venue"]): int(audit.get("missing_bars") or 0)
                                    for audit in audits
                                }
                                lines = [
                                    _recovery_line(
                                        row,
                                        missing_bars=missing_by_venue.get(
                                            str(row["venue"])),
                                    )
                                    for row in rows
                                ] + [
                                    f"**{_VENUE_NAMES.get(v, v)}**: not started"
                                    for v in self.venues if v not in seen
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
                                "The live model learns online from new 5-minute candles "
                                "as each prediction is graded; stored history is used "
                                "only by separate challenger models, none of which has "
                                "passed screening. **No model is approved for live "
                                "trading or automated signals.**"
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
                            value=fit_embed_field_value(
                                f"NO SIGNAL · {exc} "
                                "Exchange collection can be healthy while "
                                "the required continuous five-minute history "
                                "is still incomplete. Tap **Update live stats**."
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
                        pulse_raw = (
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
                        )
                        # Audit Step 2: momentum is not a forecast, and
                        # the card answers that first.
                        embed.add_field(
                            name="Current market pulse · NOT a trading signal",
                            value=fit_embed_field_value(AnswerCard(
                                question="Price momentum now",
                                verdict="Can't tell",
                                answer=(
                                    "Can't tell — past movement doesn't predict "
                                    "the next candle."
                                ),
                                reason=(
                                    f"Previous 5m median move: "
                                    f"{pulse.median_5m_pct:+.3f}%."
                                ),
                                next_step=(
                                    "Needs graded predictions, not momentum."
                                ),
                                details=(
                                    (f"{pulse.sources} of {pulse.configured} "
                                     "venues fresh."),
                                    (f"{pulse.up} up, {pulse.down} down, "
                                     f"{pulse.flat} flat."),
                                ),
                                raw=pulse_raw,
                            ).field_value()),
                            inline=False,
                        )
            if self.section == "spikes":
                try:
                    spike = await asyncio.to_thread(
                        SpikeWatch(self.market).snapshot, asset=self.asset,
                    )
                    events = spike.get("events_24h")
                    spike_raw = (
                        f"Status: **{spike['status'].replace('_', ' ')}** · "
                        f"Fresh venues: **{spike.get('fresh_venues', 0)}**.\n"
                        f"Last scan: **{_when(spike.get('checked_at'))}**.\n"
                        f"Past 24h unusual events: **"
                        f"{events if events is not None else 'unknown'}** · "
                        f"confirmed continuations: **{spike.get('scored_continuations_24h') or 0}** "
                        f"· reversals: **{spike.get('scored_reversals_24h') or 0}** "
                        f"· source-unverified: **{spike.get('unverified_24h') or 0}**.\n"
                        "Future outcomes update automatically, never backfilled "
                        "as live scores; no validated trade signal."
                    )
                    # Audit Step 2: a spike is not a prediction, and the
                    # card answers that first.
                    embed.add_field(
                        name="Spike watch analytics",
                        value=fit_embed_field_value(AnswerCard(
                            question="Spikes & volume",
                            verdict="Can't tell",
                            answer=(
                                "Can't tell — spikes don't predict what "
                                "happens next."
                            ),
                            reason=(
                                f"{events} unusual events in the past 24h."
                                if events is not None else
                                "Unusual-event count unknown for the past 24h."
                            ),
                            next_step=(
                                "Needs graded outcomes on spike events."
                            ),
                            details=(
                                f"Fresh venues: {spike.get('fresh_venues', 0)}.",
                            ),
                            raw=spike_raw,
                        ).field_value()),
                        inline=False,
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
                        value="\n".join(lines) if lines else
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
                        "The live model learns online from new 5-minute candles "
                        "as each prediction is graded. No model is approved for "
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
                        shadow_raw = (
                            "Background replay test on stored candles, separate "
                            "from live predictions: "
                            f"**{len(ready)}** exchange/timeframe streams · "
                            f"**{total:,}** replayed results (windows overlap).\n"
                            f"Probability error (Brier): **{model_score:.4f}** vs "
                            f"basic forecast **{baseline_score:.4f}** — "
                            + ("better" if model_score < baseline_score else "worse")
                            + " than the basic forecast (lower is better).\n"
                            "Research only: not a calibrated or deployed signal."
                        )
                        # Audit Step 2: the replay card answers first;
                        # replay is not live evidence, and the card says so.
                        better = model_score < baseline_score
                        shadow_card = AnswerCard(
                            question="Automatic test model evaluation",
                            verdict="Yes" if better else "No",
                            answer=(
                                "Yes — better than the coin flip in replay."
                                if better else
                                "No — worse than the coin flip in replay."
                            ),
                            reason=(
                                f"Error score {model_score:.4f} vs coin flip "
                                f"{baseline_score:.4f}."
                            ),
                            next_step=(
                                "Next: check the live graded results, not replay."
                            ),
                            details=(
                                f"{len(ready)} streams, {total:,} replayed results.",
                            ),
                            raw=shadow_raw,
                        )
                        embed.add_field(
                            name="Automatic shadow evaluation",
                            value=fit_embed_field_value(shadow_card.field_value()),
                            inline=False,
                        )
                    else:
                        shadow_raw = (
                            f"**{len(snapshots)}** tracked venue/horizon streams. "
                            f"Waiting for at least {MIN_EVALUATED} causal "
                            "settled evaluations per stream. Backfilled "
                            "candles cannot qualify as live predictions."
                        )
                        embed.add_field(
                            name="Automatic shadow evaluation",
                            value=fit_embed_field_value(AnswerCard(
                                question="Automatic test model evaluation",
                                verdict="Not yet",
                                answer="Not yet — waiting for graded replays.",
                                reason=(
                                    f"{len(snapshots)} streams tracked so far."
                                ),
                                next_step=(
                                    f"Needs {MIN_EVALUATED} graded evaluations "
                                    "per stream."
                                ),
                                raw=shadow_raw,
                            ).field_value()),
                            inline=False,
                        )
            embed.set_footer(
                text="Historical backfills do not count as live forward tests."
            )
            return embed

        if self.screen == "accuracy" and "tracker" in topics:
            polymarket = self.track_platform == "polymarket"
            if self.market is None:
                text = "Database unavailable. No tracking can be checked."
            elif polymarket:
                try:
                    result = await asyncio.to_thread(
                        PolymarketTracking(self.market.store).report,
                        owner_id=self.owner_id, asset=self.asset,
                    )
                    text = polymarket_tracking_text(result)
                except Exception:  # noqa: BLE001 - do not invent past calls
                    _LOG.exception("RivaL Polymarket tracking report unavailable")
                    text = "Could not read Polymarket tracking records; no results inferred."
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
                text = fit_embed_field_value(f"{self.message}\n\n{text}")
            embed.add_field(name="Prediction tracking", value=text, inline=False)
            embed.set_footer(
                text=("Next calls only · Polymarket · paper results, no real trades"
                      if polymarket else
                      f"Next predictions only · {self.selected_venue} · No real P&L")
            )
            if single:
                return embed

        if self.screen == "accuracy" and "progress" in topics:
            if self.market is None:
                value = "Database unavailable. No model proof can be checked."
            else:
                try:
                    current = await asyncio.to_thread(
                        proof_progress, self.market.store, asset=self.asset,
                        venue=self.selected_venue,
                    )
                    # Audit Step 2: the proof card answers first
                    # ("Is any model proven better?"), with the full
                    # per-horizon check table under "Raw numbers:".
                    value = fit_embed_field_value(proof_card(
                        current, raw_text=proof_progress_text(current),
                    ).field_value())
                except Exception:  # noqa: BLE001 - optional evidence fails closed
                    _LOG.exception("RivaL model proof progress read unavailable")
                    value = "Proof could not be checked. No qualification is assumed."
            embed.add_field(name="Future model proof", value=value, inline=False)
            # Merged proof section: the per-regime split answers the
            # same question ("is anything proving itself?") as the
            # per-family proof checks above, so it lives here now
            # instead of in a standalone "regimes" section. Both
            # kinds of evidence stay; a family's paired gain appears
            # once (in the proof checks), the regime splits carry
            # their own per-regime gains. The trial detail field is
            # the one place the numbers the Performance-page headlines
            # cut still live (vs guess, selection, coverage,
            # availability, updates, drift, gain splits) — the
            # Performance page is headline-only now.
            embed.add_field(
                name="Prospective skill by market regime",
                value=await self._regime_field_value(), inline=False,
            )
            embed.add_field(
                name="Trial detail (5-minute)",
                value=await asyncio.to_thread(self._trial_detail_value),
                inline=False,
            )
            embed.set_footer(
                text="Per-exchange future scores · No real fills or trading approval"
            )
            if single:
                return embed

        if self.screen == "accuracy":
            # These independent proof pages must render even if a separate
            # analytics query is unavailable. No generic score can hide the
            # actual forecast ledger, regime status, or execution-cost gate.
            # Only request expensive, detailed proof when the user
            # actually opens that page. This is a fresh DB query, not
            # a 5-minute stale widget or an LLM-generated number.
            if set(topics) & {"lineage", "technical"}:
                try:
                    monitor = ForwardAccuracyMonitor(self.market)
                    rows = []
                    plain_rows = []
                    proofs = []
                    for horizon in (5, 15, 60):
                        proof = await asyncio.to_thread(
                            monitor.learning_lineage_summary,
                            asset=self.asset, horizon_minutes=horizon,
                        )
                        proofs.append(proof)
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
                                f"**{_updates_text(proof)}**. "
                                f"{proof['historically_seeded']} of "
                                f"{proof['lineage_proven']} recorded forecasts "
                                "had a historical seed."
                            )
                    # Audit Step 2: the lineage section answers first
                    # ("Is it learning from history?"); the per-horizon
                    # training lines stay under "Raw numbers:".
                    if "lineage" in topics:
                        # One line per timeframe: how much it has learned live
                        # and whether history was used (the old raw block
                        # repeated all of this a second time).
                        def _lessons(horizon: int, proof: dict) -> str:
                            low = proof.get("online_training_updates_low")
                            high = proof.get("online_training_updates_high")
                            if low is None:
                                low = high = proof.get("latest_online_training_updates")
                            if low is None:
                                return f"{horizon}-min: lessons not recorded"
                            span = (f"{int(low):,}" if high in (None, low)
                                    else f"{int(low):,}–{int(high):,}")
                            return f"{horizon}-min: {span} lessons per exchange so far"

                        seen = [p for p in proofs if p.get("lineage_proven")]
                        detail_lines = tuple(
                            _lessons(horizon, proof)
                            for horizon, proof in zip((5, 15, 60), proofs)
                            if proof.get("lineage_proven")
                        )
                        if seen:
                            used = sum(int(p["historically_seeded"]) for p in seen)
                            last = sum(int(p["lineage_proven"]) for p in seen)
                            detail_lines += (
                                f"Stored history used in {used} of the last {last} predictions.",)
                        # The per-timeframe training lines stay only when
                        # nothing is proven yet (they say why).
                        lineage_value = lineage_card(
                            proofs, details=detail_lines,
                            raw=(None if detail_lines else "\n".join(plain_rows)),
                        ).field_value()
                    else:
                        lineage_value = (
                            "\n".join(rows) + "\nTraining evidence is NOT a measured win rate. "
                            "An unapproved six-month archive does not count as live model training."
                        )
                    embed.add_field(
                        name=("How is RivaL learning?" if "lineage" in topics
                              else "Model training actually used"),
                        value=fit_embed_field_value(lineage_value), inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL model training lineage unavailable")
                    embed.add_field(
                        name=("How is RivaL learning?" if "lineage" in topics
                              else "Model training actually used"),
                        value="Training history cannot be checked right now. "
                              "Do not assume historical training was used.",
                        inline=False,
                    )
            if set(topics) & {"lineage", "technical"}:
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
                    incumbent_proof = None
                    if "lineage" in topics:
                        try:
                            incumbent_proof = await asyncio.to_thread(
                                ForwardAccuracyMonitor(self.market).summary,
                                asset=self.asset,
                            )
                        except Exception:  # noqa: BLE001 - baseline read is independent
                            _LOG.exception("RivaL main model forward counters unavailable")
                    embed.add_field(
                        name=("Are advanced models ready?" if "lineage" in topics
                              else "Boosted model production proof"),
                        value=fit_embed_field_value(
                            boosted_card(
                                live_proof,
                                raw=_plain_boosted_proof(
                                    live_proof, incumbent=incumbent_proof),
                            ).field_value()
                            if "lineage" in topics else (
                            f"**Built from history:** {live_proof['portable_imported']} models, "
                            f"**{live_proof['portable_screened']}** passed screening.\n"
                            f"{explanation}\n"
                            f"**Boosted models only, last 30d:** {live_proof['issued']} "
                            f"forecasts, {live_proof['settled']} future settlements, "
                            f"{live_proof['quote_exits']} matched exit book quotes.\n"
                            f"**Most recent source:** {scope_label}. "
                            f"**Paper-qualified:** "
                            f"{len(live_proof['fresh_paper_promotions'])}.\n"
                            f"**Verdict:** {live_proof['status_reason']}.\n"
                            "Real fills, fees, funding and slippage: **NOT VERIFIED**. "
                            "GitHub research artifacts are not imported live models."
                        )),
                        inline=False,
                    )
                except Exception:  # noqa: BLE001 - source evidence fails closed
                    _LOG.exception("RivaL boosted production ledger read unavailable")
                    embed.add_field(
                        name=("Are advanced models ready?" if "lineage" in topics
                              else "Boosted model production proof"),
                        value="Cannot check model approval right now. "
                              "No model readiness or profitability can be assumed.",
                        inline=False,
                    )
            if "ledger" in topics:
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
                        value=fit_embed_field_value(
                            "\n".join(lines) if lines else
                            "No pre-outcome mathematical predictions "
                            "have been issued yet. Do not fabricate signals."
                        ),
                        inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL forward prediction ledger unavailable")
                    embed.add_field(
                        name="Prediction ledger · prospective",
                        value="Database proof unavailable; no forecast inferred.",
                        inline=False,
                    )
            if "costs" in topics:
                costs_raw = (
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
                )
                embed.add_field(
                    name="Execution cost verification",
                    value=fit_embed_field_value(
                        costs_card(raw=costs_raw).field_value()),
                    inline=False,
                )
            # A detail topic ends here, exactly as its old section did;
            # a consolidated card keeps going so its other topics' fields
            # are built too.
            if single and topics[0] in ("lineage", "technical", "ledger", "costs"):
                embed.set_footer(text="Read-only database evidence • No trading approval")
                return embed

            if "engine" in topics:
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
                            engine_state, engine_age = "unknown", None
                            engine_issued = engine_settled = 0
                        else:
                            completed = datetime.fromisoformat(
                                str(latest["completed_at"])
                            )
                            age = datetime.now(UTC) - completed.astimezone(UTC)
                            engine_issued = int(latest["math_issued"])
                            engine_settled = int(latest["math_settled"])
                            if age < timedelta(0):
                                health = "Invalid future timestamp"
                                engine_state, engine_age = "unknown", None
                            elif age > timedelta(minutes=12):
                                health = "🔴 Stale: automatic research may be stalled"
                                engine_state = "stale"
                                engine_age = int(age.total_seconds() // 60)
                            elif any(not bool(latest[key]) for key in (
                                "math_ok", "ai_settlement_ok",
                                "model_review_ok",
                            )):
                                # The retired external-AI scheduler's legacy
                                # flag is not a live stage: it must never mark
                                # a current cycle as failed.
                                health = "🟡 Cycle ran, but a stage failed"
                                engine_state = "failed"
                                engine_age = int(age.total_seconds() // 60)
                            else:
                                health = "🟢 Background research cycle completed"
                                engine_state = "running"
                                engine_age = int(age.total_seconds() // 60)
                            stages = (
                                "math " + ("OK" if latest["math_ok"] else "FAILED")
                                + " · settlement "
                                + ("OK" if latest["ai_settlement_ok"] else "FAILED")
                                + " · governance "
                                + ("OK" if latest["model_review_ok"] else "FAILED")
                                + " · retired external-AI scheduler: not run"
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
                        # Audit Step 2: the engine card answers first
                        # ("Is the engine running?"); the stage detail
                        # stays under "Raw numbers:".
                        embed.add_field(
                            name="Automatic learning engine · LIVE",
                            value=fit_embed_field_value(engine_card(
                                state=engine_state, age_minutes=engine_age,
                                issued=engine_issued, settled=engine_settled,
                                raw=heartbeat,
                            ).field_value()), inline=False,
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
                            value=fit_embed_field_value(details),
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
            if single and topics[0] == "engine":
                # Engine/worker health is a distinct live read. It must not
                # depend on slow accuracy baselines or external AI analytics.
                embed.set_footer(text="Live cycle and issuance diagnostics • Not skill proof")
                return embed
            # Audit Step 3: only the tail pieces a rendered topic needs.
            # "Is anything better?" has no summary or analytics; its
            # topics are all built above, so the tail is skipped whole.
            needs_tail = single or bool(set(topics) & {
                "overview", "five", "fifteen", "readiness", "venue",
                "calibration", "method"})
            if not needs_tail:
                embed.set_footer(
                    text="Live PostgreSQL audit • New outcomes accrue over time • "
                         "No accuracy guarantee"
                )
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
                    if set(topics) & {"readiness", "venue"}:
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
                            # Audit Step 2: the readiness card answers
                            # first ("Can RivaL predict yet?"); the
                            # per-exchange explanation stays behind the
                            # venue select as the detail layer.
                            readiness_raw = (
                                f"**{ready}/{configured}** exchange streams "
                                "eligible for 5m and 15m shadow evaluation.\n"
                                f"Longest unbroken live run: **{streak}** candles "
                                f"(needs {readiness['min_live_bars']}).\n"
                                f"Best 5m causal samples: "
                                f"**{readiness['best_causal_5m']}**, "
                                f"evaluated **{readiness['best_evaluated_5m']}** "
                                f"(minimum {readiness['min_evaluations']}).\n"
                                f"Stale/blocked: **{readiness['stale_or_blocked']}** "
                                f"· awaiting live history: "
                                f"**{readiness['waiting_history']}**.\n"
                                f"{blocker} No betting permission implied."
                            )
                            embed.add_field(
                                name="Mathematical forecast readiness · LIVE",
                                value=fit_embed_field_value(
                                    readiness_card(
                                        readiness, raw=readiness_raw,
                                    ).field_value()),
                                inline=False,
                            )
                            embed.add_field(
                                name="Why this exchange is or isn't ready",
                                value=fit_embed_field_value(explain_venue_readiness(
                                    readiness, self.selected_venue,
                                )),
                                inline=False,
                            )
                    # Audit Step 3: the horizon summaries belong
                    # to "At a glance" / "How accurate is it?"
                    # (and the old single-topic views); "Is it
                    # running?" and "Would it make money?" do not
                    # show them.
                    if single or set(topics) & {"overview", "five", "fifteen"}:
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
                                    rate_or_too_few(
                                        scored,
                                        f"{stats['hit_rate'] * 100:.1f}%",
                                        what="scored")
                                    if stats["hit_rate"] is not None else "unavailable"
                                )
                                value = (
                                    f"Issued: **{stats['issued']:,}**, settled: "
                                    f"**{n:,}** across {stats['venues']} exchanges.\n"
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
                                value=fit_embed_field_value(
                                    _forward_eval_card(horizon, stats, scored, value)),
                                inline=False,
                            )
                        # Never replace verified out-of-sample results with a
                        # compelling-looking near-term price move. A short period
                        # of market collection is not a validated betting edge.
                        recent_5 = report["last_7d"][5]
                        recent_15 = report["last_7d"][15]
                        window = "the last 7 days"
                        try:
                            first = min(
                                datetime.fromisoformat(str(x)) for x in (
                                    recent_5.get("first_issued"), recent_15.get("first_issued"),
                                ) if x
                            )
                            age = datetime.now(UTC) - first
                            if first.tzinfo is not None and age < timedelta(days=6, hours=12):
                                window = (f"the {max(1, round(age.total_seconds() / 86400))} days "
                                          f"since this model went live ({first:%b %d})")
                        except (TypeError, ValueError):
                            pass
                        embed.add_field(
                            name="Research verdict",
                            value=fit_embed_field_value(
                                "**NO VERIFIED BETTING EDGE.** RivaL may observe "
                                "live prices and issue shadow predictions, but "
                                f"only **{recent_5['settled']:,}** five-minute and "
                                f"**{recent_15['settled']:,}** fifteen-minute "
                                "prospective forecasts have been graded in "
                                f"{window}. Real fill costs, funding, "
                                "slippage and liquidation risk are not fully "
                                "validated. No auto-betting orders are placed. "
                                "Do not interpret a green feed or historical "
                                "momentum as a recommended trade."
                            ), inline=False,
                        )
                        # Overview and horizon summaries already have their verified
                        # prospective results; advanced analytics belongs to its own section.
                        if single and topics[0] in ("overview", "five", "fifteen", "readiness", "venue"):
                            return embed
                    # Audit Step 3: the analytics + calibration
                    # detail belongs to "How accurate is it?" and
                    # "Would it make money?" (and the old
                    # single-topic views).
                    if set(topics) & {"calibration", "method"}:
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
                                late = int((details.get("issued_after_window_opened_7d") or {})
                                           .get(horizon, 0))
                                late_note = (
                                    f"⚠️ {late:,} graded {horizon}-minute predictions are not "
                                    "counted here: each was issued after its window had "
                                    "already started (using only data from before it).\n"
                                    if late and not recent["events"] else ""
                                )
                                embed.add_field(
                                    name=f"Math {horizon}m · independent UTC windows",
                                    value=fit_embed_field_value(
                                        late_note +
                                        f"**{recent['events']}** forward events, "
                                        f"**{details['math_pending']}** awaiting results "
                                        "(all exchanges and timeframes).\n"
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
                                    ),
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
                                late = int((details.get("issued_after_window_opened_7d") or {})
                                           .get(horizon, 0))
                                calibration_raw = (
                                    (f"ℹ️ Not measured here, by design: each {horizon}-minute "
                                     "prediction is issued about 1–2 minutes after its "
                                     "result window opens (the model only uses closed "
                                     "candles, so its accuracy score elsewhere is fair, "
                                     "but nobody could have traded at that price). "
                                     f"Calibration counts only predictions made before the "
                                     f"window, and none of the {late:,} graded ones were.\n"
                                     if late and not reliability["unique_targets"] else "") +
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
                                ).replace("\\n", "\n")
                                embed.add_field(
                                    name=f"Forward {horizon}m calibration",
                                    value=fit_embed_field_value(
                                        _calibration_card(
                                            horizon, reliability, calibration_raw)),
                                    inline=False,
                                )
                            ai_status = details["historical_external_ai"]
                            quality = (
                                rate_or_too_few(
                                    int(ai_status["decided"] or 0),
                                    f"{ai_status['directional_accuracy'] * 100:.1f}%",
                                    what="decided")
                                if ai_status["directional_accuracy"] is not None
                                else "not measurable"
                            )
                            embed.add_field(
                                name="Historical external AI · forward outcomes",
                                value=fit_embed_field_value(
                                    f"Issued **{ai_status['issued']}**, "
                                    f"settled **{ai_status['settled']}**, "
                                    f"directional decisions **{ai_status['decided']}**, "
                                    f"uncertain/abstain **{ai_status['abstained']}**.\n"
                                    f"Observed hit rate **{quality}**. "
                                    "Self-reported confidence is NOT calibrated. "
                                    "Source health and provider availability may "
                                    "cause skipped cycles."
                                ),
                                inline=False,
                            )
                        if "method" in topics or single:
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
                usage = (
                    f"{state['requests_today']}/{state['rival_daily_limit']} requests today"
                    if state['rival_daily_limit'] else
                    f"{state['requests_today']} requests today; no daily cap"
                )
                embed.add_field(
                    name="Private LFM3 Intelligence",
                    value=fit_embed_field_value(
                        f"Model: **{discord.utils.escape_markdown(state['model'])}**\n"
                        + (
                            "Configured for on-demand inference. **No paid tokens "
                            "or external fallback.**\n"
                            f"Local LFM3 usage: **{usage}**, "
                            "60 s minimum spacing, 3 per requester per day. "
                            "Background loops run only with operator opt-in.\n"
                            "The inference service queues/limits CPU work. "
                            "Live host readiness is not checked on this screen."
                            if configured else
                            "Private RIVAL_LOCAL_AI_TOKEN is not configured. "
                            "AI research is unavailable; mathematical learning continues."
                        )
                    ),
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
                value=fit_embed_field_value(
                    f"Direction: **{answer.direction.title()}** · "
                    f"{answer.confidence} self-reported confidence\n"
                    f"Feeds: {answer.source_count} · "
                    f"{'Database cache' if answer.from_cache else 'New response'}\n"
                    f"Answered by: **{_answered_by(answer.model)}**\n"
                    f"Reason: {discord.utils.escape_mentions(answer.thesis)}\n"
                    f"Risk: {discord.utils.escape_mentions(answer.risk)}"
                ), inline=False,
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
            target_service=self.target_service, watch_service=self.watch_service,
        )
        fresh.compare = list(self.compare)
        fresh.asset = self.asset
        fresh.screen = self.screen
        fresh.section = self.section
        fresh.detail = self.detail
        fresh.model = self.model
        fresh._model_cache = self._model_cache
        fresh.selected_venue = self.selected_venue
        fresh.track_platform = self.track_platform
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

        async def slow() -> None:
            # Only the newest press may say "still loading" on this message.
            if state["revision"] == revision:
                await asyncio.wait_for(interaction.edit_original_response(
                    embed=still_loading(), view=fresh), timeout=5)

        try:
            embed = await patient(fresh.render, on_slow=slow)
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
                    "This database check did not finish in 45 seconds. "
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
        # Audit Step 3: a stale section key resolves before the loading
        # title looks it up in _SECTIONS.
        self.section = _resolve_section(self.screen, self.section)
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

