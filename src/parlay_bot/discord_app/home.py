"""Private, guided RivaL home for ordinary users, without losing advanced features."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.alert_control import stop_all_notifications
from parlay_bot.discord_app.api_limits import OwnerLimitsView
from parlay_bot.discord_app.cdna_public import CDNAPersonalReader
from parlay_bot.discord_app.cdna_ui import (
    CDNAPersonalModal,
    cdna_coin_embed,
    verify_cdna_owner,
)
from parlay_bot.discord_app.kalshi_public import KalshiCryptoBrowser
from parlay_bot.discord_app.manual_parlay import ManualParlayView
from parlay_bot.discord_app.out_the_gate import below_line_label, lean_short
from parlay_bot.discord_app.polymarket_live_board import live_polymarket_embed
from parlay_bot.discord_app.presentation import (
    parlay_embed,
)
from parlay_bot.discord_app.signals import model_strength, signal_embed
from parlay_bot.discord_app.targeting import target_snapshot
from parlay_bot.jurisdiction.zip_lookup import ZipLookupError
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import QuoteUnavailable, quote_embed
from parlay_bot.perpetuals.markets import ASSET_NAMES
from parlay_bot.perpetuals.markets import ASSETS as RESEARCH_ASSETS
from parlay_bot.perpetuals.markets import HORIZONS as RESEARCH_HORIZONS
from parlay_bot.providers.owner_limits import OwnerLimitStore

if TYPE_CHECKING:
    from parlay_bot.collectors.current import CurrentBoardCollector
    from parlay_bot.discord_app.auto_hunt import AutoHuntService
    from parlay_bot.discord_app.targeting import (
        DirectionTargetService,
        TargetSnapshot,
    )
    from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
    from parlay_bot.parlays.builder import ParlayBuilder
    from parlay_bot.perpetuals.live_quotes import PublicBestQuoteClient
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
    from parlay_bot.perpetuals.paper_ledger import PaperLedger
    from parlay_bot.perpetuals.profit_proof import ProspectiveQuoteProfitMonitor
    from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger

_LOG = logging.getLogger(__name__)
# The event loop keeps only weak references to tasks. Hold fire-and-forget
# menu work here until it finishes so it cannot be garbage-collected mid-run.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro, *, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
COINS = RESEARCH_ASSETS
MARKET_NAMES = {
    "polymarket": "Polymarket Up/Down",
    "exchange": "Crypto perpetual futures",
    "kalshi": "Kalshi event contracts",
    "draftkings": "DraftKings Predictions • NOT CONNECTED",
}
EXPLAIN = {
    "home": "Crypto and Sports let you look at markets. Bot Performance shows how often RivaL's predictions were right. Practice uses pretend money only.",
    "discover": "Pick Crypto or Practice. Nothing here places a real trade.",
    "crypto-desk": "Pick the app you actually use. Prediction markets (Polymarket, Kalshi, DraftKings Predictions) work differently from crypto exchanges (Binance, Hyperliquid, OKX and others), so each one gets its own predictions and its own score.",
    "live": "Get one private message about a Polymarket market that updates only when something important changes. A market's price is not a guarantee of how it ends.",
    "hunt": "Follow a Polymarket market or an exchange prediction in your private messages. Stop All turns every personal update off.",
    "hunt-tools": "These old notifications were replaced. Use Notifications.",
    "kalshi": "Kalshi is a separate US prediction market with its own prices and rules, so RivaL scores it separately from Polymarket.",
    "sports": "Sportsbook odds show what a bet could pay, not whether it will win. PASS means RivaL has no pick it trusts. Choose your state to see the sportsbooks you can use.",
    "slip": "A parlay combines several picks, and every one of them has to win. No real bet is placed here.",
    "crypto": "Pick a coin and a timeframe. UP or DOWN is RivaL's guess, not advice to trade. A price target is a price to watch for, not a promise.",
    "forecast": "This is RivaL's guess about where the price goes next, not a proven money-maker. NO SIGNAL means the guess is too weak to show. Its track record and when it expires are listed.",
    "quote": "Ask is about what you'd pay to buy; bid is about what you'd get to sell. Real orders can fill at a different price.",
    "practice": "Practice uses pretend money only. Buy if you think the price will rise, sell if you think it will fall. Pretend wins never count as real profit.",
    "results": "These are your own pretend trades. They never count toward the bot's prediction record.",
    "performance": "Shows how often RivaL's saved predictions turned out right, whether it beats a simple guess, and whether everything is running. Exchange and Polymarket predictions are scored separately.",
    "help": "Crypto covers prediction markets and crypto exchanges. Sports covers sportsbook odds. DraftKings sports futures are sports bets, not crypto.",
    "advanced": "Extra research, how much of each data service is being used, and exact-price alerts. Accuracy and learning are on Bot Performance.",
    "feeds": "This screen moved: Bot Performance now shows whether price feeds and predictions are running.",
    "trust": "This screen moved to Bot Performance. Note that being busy and getting more accurate are not the same thing.",
}

# Sports & Odds page scope note. The research model reads past final scores
# only; live sportsbook odds are never a model input, so the page must not
# imply a market-aware sports read.
SPORTS_MODEL_SCOPE_TEXT = (
    "Sports & Odds shows sportsbook prices and screens, "
    "not model predictions. The research model covers "
    "soccer and basketball only, once a league has 130 "
    "finalized games. NFL has odds and slips but no model "
    "forecast. The research model reads past final scores "
    "only; it does not use live odds."
)


class _Button(discord.ui.Button["GuidedHome"]):
    def __init__(self, title: str, action: str, row: int,
                 emoji: str | None = None, primary: bool = False) -> None:
        if action == "forecast":
            style = discord.ButtonStyle.success
        elif action in ("stop", "hunt-off", "poly-stop", "stop-all"):
            style = discord.ButtonStyle.danger
        else:
            style = (
                discord.ButtonStyle.primary
                if primary else discord.ButtonStyle.secondary
            )
        super().__init__(
            label=title, custom_id=f"rival:guided:{action}", emoji=emoji,
            row=row, style=style,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        started = time.monotonic()
        try:
            await self.view.act(interaction, self.action)
        finally:
            _LOG.info("Guided button action=%s total_ms=%.0f acknowledged=%s",
                      self.action, (time.monotonic() - started) * 1000,
                      interaction.response.is_done())


class _Market(discord.ui.Select["GuidedHome"]):
    """Never preselect Polymarket for a new user."""

    def __init__(self, selected: str | None) -> None:
        super().__init__(
            placeholder="1. Which platform are you using?", row=0,
            custom_id="rival:guided:market",
            options=[
                discord.SelectOption(
                    label="Polymarket", description="Crypto Up/Down event contracts",
                    value="polymarket", default=selected == "polymarket",
                ),
                discord.SelectOption(
                    label="Crypto Perpetuals", description="Binance, OKX, Hyperliquid; NOT DraftKings",
                    value="exchange", default=selected == "exchange",
                ),
                discord.SelectOption(
                    label="Kalshi research", description="Contract prices and experimental model; no approved call",
                    value="kalshi", default=selected == "kalshi",
                ),
                discord.SelectOption(
                    label="DraftKings Predictions",
                    description="Crypto event contracts; NOT connected yet",
                    value="draftkings", default=selected == "draftkings",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.live_source = self.values[0]
        self.view.note = None
        await interaction.response.defer()
        await self.view.redraw(interaction)


class _Coin(discord.ui.Select["GuidedHome"]):
    def __init__(self, selected: str, *, row: int = 0) -> None:
        names = ASSET_NAMES
        super().__init__(
            placeholder="Choose a coin", row=row, custom_id="rival:guided:coin",
            options=[
                discord.SelectOption(
                    label=f"{names[c]} ({c})", value=c, default=c == selected,
                ) for c in COINS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.coin = self.values[0]
        self.view.note = None
        await interaction.response.defer()
        await self.view.redraw(interaction)


class _Horizon(discord.ui.Select["GuidedHome"]):
    def __init__(self, selected: int, *, row: int = 1) -> None:
        super().__init__(
            placeholder="Choose how far ahead", row=row,
            custom_id="rival:guided:horizon",
            options=[
                discord.SelectOption(
                    label=f"{n} minutes", value=str(n), default=n == selected,
                ) for n in RESEARCH_HORIZONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.horizon = int(self.values[0])
        self.view.note = None
        await interaction.response.defer()
        await self.view.redraw(interaction)


class _PolymarketHorizon(discord.ui.Select["GuidedHome"]):
    """Three research horizons; do not imply another venue lists them."""

    def __init__(
        self, selected: int, *, row: int = 1, market: str = "polymarket",
    ) -> None:
        super().__init__(
            placeholder=(
                "Choose an independent research horizon"
                if market != "polymarket" else "Choose a listed market window"
            ), row=row,
            custom_id="rival:guided:horizon",
            options=[
                discord.SelectOption(
                    label="1 hour" if n == 60 else f"{n} minutes", value=str(n), default=n == selected,
                ) for n in (5, 15, 60)
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        self.view.horizon = int(self.values[0])
        self.view.note = None
        await interaction.response.defer()
        await self.view.redraw(interaction)


class _Entry(discord.ui.Modal):
    def __init__(self, home: GuidedHome, kind: str) -> None:
        settings = {
            "zip": ("Enter ZIP", "Your 5-digit ZIP", "06001", 5, 5),
            "state": ("Choose State", "State name or letters", "CT", 2, 32),
            "target": ("Watch Price", "Exact USDT price", "75000", 1, 28),
            "funds": ("Practice Funds", "Fake starting amount (100 to 100000)", "10000", 1, 24),
        }
        title, label, placeholder, minimum, maximum = settings[kind]
        super().__init__(title=title, timeout=300)
        self.home = home
        self.kind = kind
        self.value_input = discord.ui.TextInput(
            label=label, placeholder=placeholder,
            min_length=minimum, max_length=maximum,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.home.owner_id:
            await interaction.response.send_message(
                "This is someone else's menu. Use /rival for your own.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.home.submit(interaction, self.kind, str(self.value_input.value))


def _parlay_diagnostics_text(diagnostics: dict[str, int]) -> str:
    """Plain-language summary of why the last slip build returned PASS.

    Only nonzero gates are listed, most informative first. An empty string
    means the counters name no gate, in which case the UI shows no field
    rather than a made-up reason.
    """
    if diagnostics.get("rows_seen", 0) == 0:
        return "No fresh eligible prices were found."
    labels = (
        ("below_advertised_edge", "below the 2% advertised-edge screen"),
        ("insufficient_reference_books", "lacked 2 reference books"),
        ("negative_edge", "negative edge"),
        ("below_minimum_probability", "below the minimum probability"),
        ("books_short_of_legs", "sportsbooks short of enough independent fixtures"),
        ("duplicate_fixture_skips", "duplicate-fixture legs skipped"),
        ("single_outcome_groups", "markets with only one outcome side"),
        ("invalid_probability", "markets with unusable prices"),
        ("duplicate_vectors", "duplicate price sets skipped"),
    )
    parts = [
        f"{diagnostics[key]} {label}"
        for key, label in labels
        if diagnostics.get(key, 0) > 0
    ]
    return " • ".join(parts)




def _performance_exchange(market: object, asset: str) -> dict:
    """Fast seven-day forecast score, independent of deeper research reads."""
    return ForwardAccuracyMonitor(market).summary(asset=asset)


def score_consistency_problems(part: dict) -> list[str]:
    """Return every way a score summary contradicts itself (empty = consistent).

    Each count is a subset of the one before it, so a larger child count
    means a query or settlement bug, never a real result. The page shows a
    warning and logs an ERROR (which reaches the status card) instead of
    silently presenting impossible numbers.
    """
    def count(key: str, default: int = 0) -> int:
        try:
            return int(part.get(key, default) or 0)
        except (TypeError, ValueError):
            return -1

    issued, settled = count("issued"), count("settled")
    scored = count("scored", settled)
    decided, correct = count("decided"), count("correct")
    problems = [f"{name} is negative" for name, value in (
        ("issued", issued), ("settled", settled), ("scored", scored),
        ("decided", decided), ("correct", correct),
    ) if value < 0]
    chain = (("settled", settled, "issued", issued), ("scored", scored, "settled", settled),
             ("decided", decided, "scored", scored), ("correct", correct, "decided", decided))
    problems += [f"{a} {x:,} > {b} {y:,}" for a, x, b, y in chain if x > y >= 0]
    if "due" in part:
        due, overdue = count("due"), count("overdue")
        if due > issued:
            problems.append(f"due {due:,} > issued {issued:,}")
        if overdue > max(0, due - settled):
            problems.append(f"overdue {overdue:,} > unsettled due {max(0, due - settled):,}")
        if settled > due >= 0:
            problems.append(f"settled {settled:,} > due {due:,} (settled before the result existed)")
    hit = part.get("hit_rate")
    if isinstance(hit, (int, float)):
        if not 0 <= hit <= 1:
            problems.append(f"hit rate {hit} outside 0-1")
        elif decided > 0 and abs(hit - correct / decided) > 0.001:
            problems.append(f"hit rate {hit:.3f} != {correct}/{decided}")
    for key in ("brier", "baseline_brier"):
        value = part.get(key)
        if isinstance(value, (int, float)) and not 0 <= value <= 1:
            problems.append(f"{key} {value} outside 0-1")
    return problems


def _performance_benefit(market: object, asset: str, horizon: int) -> dict:
    """Score improvement independently of the faster historical-lineage query."""
    return ForwardAccuracyMonitor(market).active_learning_evidence(
        asset=asset, horizon_minutes=horizon,
    )


# The improvement query takes 3-7 s in production, longer than the page's
# 4 s budget, and its answer moves slowly. The bot refreshes every coin and
# timeframe in the background (warm_benefit_cache) so the page reads a ready
# answer; a page request that still misses waits for any run already in
# flight instead of starting a duplicate query.
BENEFIT_CACHE_SECONDS = 45 * 60
# The Performance page renders in the background with an 8 s overall budget
# (_finish_performance), so each independent reader may use most of it.
# Production score queries take 2-7 s under load; 4 s made them fail often.
PERFORMANCE_READ_SECONDS = 7.0
_BENEFIT_CACHE: dict[tuple, tuple[float, object, dict]] = {}
_BENEFIT_INFLIGHT: dict[tuple, threading.Event] = {}
_BENEFIT_LOCK = threading.Lock()


def _cached_benefit(market: object, asset: str, horizon: int, *, refresh: bool = False) -> dict:
    key = (id(market), asset, horizon)
    while True:
        with _BENEFIT_LOCK:
            hit = _BENEFIT_CACHE.get(key)
            if (not refresh and hit is not None and hit[1] is market
                    and time.monotonic() - hit[0] < BENEFIT_CACHE_SECONDS):
                return hit[2]
            running = _BENEFIT_INFLIGHT.get(key)
            if running is None:
                running = _BENEFIT_INFLIGHT[key] = threading.Event()
                break
        running.wait(timeout=30)
        refresh = False  # the other run's result is fresh enough
    try:
        result = _performance_benefit(market, asset, horizon)
        if isinstance(result, dict):
            with _BENEFIT_LOCK:
                if len(_BENEFIT_CACHE) > 64:
                    _BENEFIT_CACHE.clear()
                _BENEFIT_CACHE[key] = (time.monotonic(), market, result)
        return result
    finally:
        with _BENEFIT_LOCK:
            _BENEFIT_INFLIGHT.pop(key, None)
        running.set()


def warm_benefit_cache(market: object) -> int:
    """Background refresh of every coin/timeframe improvement check."""
    done = 0
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        for horizon in (5, 15, 60):
            try:
                _cached_benefit(market, asset, horizon, refresh=True)
                done += 1
            except Exception:  # noqa: BLE001 - optional page context only
                _LOG.warning("Improvement check refresh failed %s %s min", asset, horizon)
    return done


def _pooled_record(market: object, asset: str, horizon: int) -> dict:
    """Live graded record of the pooled multi-year challenger now in use (7 days).

    Only the newest model family counts (older versions are separate tests),
    only gradable results count (a target with no trades has no result), and
    the sample is reported as distinct target windows: the exchanges move
    together, so one window across nine exchanges is about one observation.
    """
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with market.store.connect() as conn:
        try:
            latest = conn.execute(
                """SELECT family FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family LIKE 'pooled_%'
                     AND issued_at>=? ORDER BY issued_at DESC LIMIT 1""",
                (asset, horizon, since),
            ).fetchone()
            if latest is None:
                return {"issued": 0}
            row = conn.execute(
                """SELECT COUNT(*) AS issued,
                          SUM(CASE WHEN outcome_up IN (0,1) THEN 1 ELSE 0 END) AS graded,
                          SUM(CASE WHEN outcome_up IN (0,1) AND probability_up<>0.5 AND
                                ((probability_up>0.5 AND outcome_up=1) OR
                                 (probability_up<0.5 AND outcome_up=0)) THEN 1 ELSE 0 END) AS hits,
                          SUM(CASE WHEN outcome_up IN (0,1) AND probability_up<>0.5
                                   THEN 1 ELSE 0 END) AS decided,
                          COUNT(DISTINCT CASE WHEN outcome_up IN (0,1)
                                THEN target_opened_at END) AS windows,
                          AVG(CASE WHEN outcome_up IN (0,1) THEN model_brier END) AS model_brier,
                          AVG(CASE WHEN outcome_up IN (0,1) THEN incumbent_brier END)
                              AS incumbent_brier,
                          AVG(CASE WHEN outcome_up IN (0,1) THEN baseline_brier END)
                              AS baseline_brier,
                          MIN(issued_at) AS first_issued
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND family=? AND issued_at>=?""",
                (asset, horizon, latest["family"], since),
            ).fetchone()
        except Exception:  # noqa: BLE001 - table appears after first challenger issue
            return {"issued": 0}
    return {k: row[k] for k in ("issued", "graded", "hits", "decided", "windows",
                                 "model_brier", "incumbent_brier", "baseline_brier",
                                 "first_issued")}


def _regime_record(market: object, asset: str, horizon: int) -> dict:
    """Live graded record of the regime-gated challenger (7 days).

    Same columns as the pooled trial row, measured on regime_v1's issued
    subset only, plus the D5 selection decomposition (paired_gain,
    selection_effect, coverage) so the page shows whether the family won
    on the targets it chose or only by choosing easy ones.
    """
    from parlay_bot.perpetuals.regime_model import trial_report

    return trial_report(market.store, asset=asset, horizon=horizon)


def _regime_live_record(market: object, asset: str, horizon: int) -> dict:
    """Live graded record of the regime live-learning challenger (30 days).

    Same columns as the regime_v2 trial row, measured on
    regime_live_v1's issued subset only, plus the D7 extras: the
    trending-label update count, the weight drift from the warm-start
    copy, and the paired-gain split (last 7 days vs days 8-30).
    """
    from parlay_bot.perpetuals.regime_live_model import BASE_FAMILY, trial_report

    return trial_report(market.store, asset=asset, horizon=horizon, family=BASE_FAMILY)


def _regime_live_fast_record(market: object, asset: str, horizon: int) -> dict:
    """Live graded record of the fast live-learning challenger (30 days).

    Same columns as _regime_live_record, for regime_live_fast_v1 (the
    2x learning-rate family, graded separately).
    """
    from parlay_bot.perpetuals.regime_live_model import FAST_FAMILY, trial_report

    return trial_report(market.store, asset=asset, horizon=horizon, family=FAST_FAMILY)


def _trial_clock(record, now):
    """(days, targets) for the one-line clock, from the trial record itself.

    targets = distinct decided windows (the same measure the Route B
    evaluation calls "targets": one independent observation per target
    window). days = days since first issuance in this record's window.
    Both are measured over the trial window — 7 days for the pooled and
    regime_v1 rows, 30 days for the live families — so for a family older
    than its window the day count is capped by the window. The
    authoritative 30-day Route B clock is the one the status card and
    the Research dashboard show per scope.
    """
    targets = int(record.get("windows") or 0)
    days = 0
    try:
        first = datetime.fromisoformat(str(record.get("first_issued")))
        if first.tzinfo is None:
            first = first.replace(tzinfo=UTC)
        days = max(1, (now - first.astimezone(UTC)).days)
    except (TypeError, ValueError):
        days = 0
    return days, targets


def _trial_one_liner(label, record, now):
    """One family, one headline line + one short detail line.

    Headline: name · accuracy · Brier vs incumbent · paired gain ·
    clock. Detail line: everything else the old paragraph showed —
    Brier vs the simple guess, vs-regime_v1 head-to-head, selection,
    coverage, availability, trending labels learned, weight drift,
    the 7d vs 8-30d gain split, and "too early to judge". Nothing is
    dropped, only shortened; a metric the record does not carry is
    simply absent from the detail line. Returns "" when the family has
    not issued yet (renders only when issued>0, like every other
    trial row).
    """
    if not isinstance(record, dict) or not int(record.get("issued") or 0):
        return ""
    issued = int(record["issued"])
    graded, decided = int(record.get("graded") or 0), int(record.get("decided") or 0)
    days, targets = _trial_clock(record, now)
    clock = f" · {targets:,}/150 targets" + (f" · day {days}/21" if days else "")
    if not (decided and record.get("model_brier") is not None
            and record.get("incumbent_brier") is not None
            and record.get("baseline_brier") is not None):
        return (f"**{label}:** {issued:,} predictions made, "
                f"{graded:,} scored so far{clock}")
    hits = int(record.get("hits") or 0)
    gain = float(record["incumbent_brier"]) - float(record["model_brier"])
    headline = (
        f"**{label}:** right {hits:,} of {decided:,} ({hits / decided:.1%})"
        f" · Brier {float(record['model_brier']):.3f} vs incumbent "
        f"{float(record['incumbent_brier']):.3f} · paired gain {gain:+.3f}{clock}"
    )
    details = [f"vs guess {float(record['baseline_brier']):.3f}"]
    gain_v1 = record.get("paired_gain_vs_regime_v1")
    if gain_v1 is not None:
        details.append(f"vs regime_v1 {float(gain_v1):+.3f}")
    sel = record.get("selection_effect")
    if sel is not None:
        details.append(
            f"selection {float(sel):+.3f} "
            f"({'harder' if float(sel) > 0 else 'easier'} subset)")
    cov = record.get("coverage")
    if cov is not None:
        details.append(f"coverage {float(cov):.0%}")
    avail = record.get("availability_ratio")
    if avail is not None:
        details.append(f"availability {float(avail):.0%}")
    updates = record.get("updates")
    if updates is not None:
        details.append(f"{int(updates):,} trending labels learned")
    drift = record.get("weight_drift_mean")
    if drift is not None:
        details.append(f"weight drift {float(drift):.3f}")
    gain_7d = record.get("paired_gain_last_7d")
    if gain_7d is not None:
        details.append(f"gain last 7d {float(gain_7d):+.3f}")
    gain_8_30 = record.get("paired_gain_days_8_30")
    if gain_8_30 is not None:
        details.append(f"gain days 8-30 {float(gain_8_30):+.3f}")
    if int(record.get("windows") or 0) < 300:
        details.append("too early to judge")
    return headline + "\n" + " · ".join(details)


def _regime_live_text(record, label, now=None):
    """One live family's trial row as a performance-page fragment.

    The one-line format (see _trial_one_liner): headline + short
    detail line, same as the pooled and regime_v1 rows. Empty when the
    family has not issued yet (renders only when issued>0, like every
    other trial row).
    """
    if not isinstance(record, dict) or not int(record.get("issued") or 0):
        return ""
    return "\n" + _trial_one_liner(label, record, now or datetime.now(UTC))


def _intelligence_status(market: object, asset: str, horizon: int) -> dict | None:
    """RivaL Intelligence's graded reviews of the models (None if never run)."""
    from parlay_bot.perpetuals.intelligence import IntelligenceLedger

    ledger = IntelligenceLedger(market.store)
    latest = ledger.latest(asset=asset)
    if latest is None:
        return None
    return {"latest": latest, "report": ledger.report(asset=asset, horizon=horizon)}


def _cycle_coverage(market: object) -> dict | None:
    """How many of the last 24 hours' 5-minute prediction cycles really ran."""
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    try:
        with market.store.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cycles, COALESCE(SUM(target_expired), 0) AS expired, "
                "COALESCE(SUM(issued_forecasts), 0) AS issued "
                "FROM perp_forward_cycle_diagnostics WHERE checked_at>=?",
                (since,),
            ).fetchone()
        return {"cycles": int(row["cycles"] or 0), "expired": int(row["expired"] or 0),
                "issued": int(row["issued"] or 0)}
    except Exception:  # noqa: BLE001 - optional context line, never blocks the page
        _LOG.warning("Performance cycle coverage read unavailable")
        return None


def _performance_lineage(market: object, asset: str, horizon: int) -> dict:
    """Report verified training provenance even when trend scoring times out."""
    return ForwardAccuracyMonitor(market).learning_lineage_summary(
        asset=asset, horizon_minutes=horizon,
    )


def _latest_learning_cycle(market: object) -> dict | None:
    """Tiny persisted heartbeat query; no offline registry or Drive work in Discord."""
    try:
        with market.store.connect() as conn:
            row = conn.execute(
                "SELECT completed_at, math_ok, model_review_ok, math_issued, math_settled "
                "FROM perp_learning_cycle_health ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
    except Exception:  # noqa: BLE001 - a missing/busy table is not a healthy cycle
        _LOG.exception("Performance learning heartbeat read unavailable")
        return {"status": "unavailable"}
    if row is None:
        return None
    try:
        when = datetime.fromisoformat(str(row["completed_at"]))
        if when.tzinfo is None:
            raise ValueError("heartbeat timestamp has no timezone")
        return {
            "status": "recorded", "checked_at": when,
            "math_ok": bool(int(row["math_ok"])),
            "model_review_ok": bool(int(row["model_review_ok"])),
            "issued": int(row["math_issued"]), "settled": int(row["math_settled"]),
        }
    except (TypeError, ValueError, OverflowError, KeyError):
        _LOG.warning("Performance learning heartbeat contains invalid data")
        return {"status": "unavailable"}


def _performance_cycles(market: object) -> dict:
    """Read independent persisted worker heartbeats without expensive training."""
    # Each reader owns its own database checkout. A failed/missing forecast
    # receipt must never erase an independently persisted learning receipt.
    return {
        "forecast": _latest_forward_cycle(market),
        "learning": _latest_learning_cycle(market),
    }


def _latest_forward_cycle(market: object) -> dict | None:
    """Most recent forecast issuance cycle; never infer health from fresh feeds."""
    try:
        with market.store.connect() as conn:
            row = conn.execute(
                "SELECT checked_at, issued_forecasts, target_expired "
                "FROM perp_forward_cycle_diagnostics "
                "ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
    except Exception:  # noqa: BLE001 - operational failure is not zero activity
        _LOG.exception("Forward-cycle diagnostics read failed")
        return {"status": "unavailable"}
    if row is None:
        return None
    try:
        checked = datetime.fromisoformat(str(row["checked_at"]))
        if checked.tzinfo is None:
            raise ValueError("forecast heartbeat timestamp has no timezone")
        issued, expired = int(row["issued_forecasts"] or 0), int(row["target_expired"] or 0)
        if issued < 0 or expired < 0:
            raise ValueError("negative forecast diagnostics")
    except (ValueError, TypeError, OverflowError, KeyError):
        _LOG.warning("Invalid forecast-cycle diagnostics row")
        return {"status": "unavailable"}
    return {"status": "recorded", "checked_at": checked, "issued": issued, "expired": expired}


def _crypto_preview_headline(snapshot: TargetSnapshot | None) -> str:
    """One honest line for the crypto page preview.

    The old fallback ("NO CURRENT SIGNAL • Preview pending") made every
    no-forecast state read as a broken bot. target_snapshot already
    distinguishes the states; this maps each one to its own line instead
    of discarding that detail. The preview path deliberately does NOT
    query feed health itself — it must never wait on the contended pool —
    so a stale feed surfaces here as "no current forecast", with the
    per-venue staleness detail staying on the Feeds page, which already
    shows last_success per venue.
    """
    if snapshot is None:
        return "Preview timed out — Get Signal checks live"
    if snapshot.probability is not None:
        return (
            f"{snapshot.direction} lean • raw "
            f"{snapshot.probability:.1%} ({model_strength(snapshot.probability)})"
        )
    if snapshot.status == "tied":
        return "No directional call — model tied"
    if snapshot.status == "no_forecast":
        return "No current forecast — markets being collected"
    if snapshot.status == "collector_down":
        return "Collector unavailable — signals paused"
    if snapshot.status == "feed_error":
        return "Feed check failed — Get Signal verifies live"
    return snapshot.detail



def crypto_snapshot_card(
    snapshot: TargetSnapshot | None, *, asset: str, horizon: int,
    checked_at: datetime | None = None,
) -> discord.Embed:
    """Compact four-tile Discord model card without fabricated financial stats.

    Snapshot.price is the MODEL'S REFERENCE CANDLE, not a current order-book
    quote or the Polymarket settlement strike. A complete venue-matched signal
    check is needed for the actual bid/ask and the measured 7-day hit rate.
    """
    stamp = checked_at or datetime.now(UTC)
    # D1(a): a sub-threshold lean (weak_model) or an unqualified model
    # (unqualified_model) is still a displayable prediction. The WATCH
    # verdict is unchanged; only the card's display widens.
    current = (
        snapshot is not None
        and snapshot.status in ("ok", "weak_model", "unqualified_model")
        and snapshot.asset == asset and snapshot.horizon_minutes == horizon
        and snapshot.ends_at is not None and snapshot.ends_at > stamp
        and snapshot.issued_at is not None and snapshot.issued_at <= stamp
        and snapshot.direction in ("UP", "DOWN")
    )
    status = (
        f"{snapshot.direction} LEAN • RESEARCH ONLY"
        if current else "WAIT • NO SIGNAL"
    )
    embed = discord.Embed(
        title=f"{ASSET_NAMES.get(asset, asset)} • {horizon} min | {status}",
        description=(
            "Exchange-candle model issued "
            + discord.utils.format_dt(snapshot.issued_at, style="R")
            + ". This is not a Polymarket win prediction."
            if current else
            "**No verified current direction.** "
            + (
                "The previous forecast has expired; checking for a new window."
                if snapshot is not None and snapshot.status == "ok"
                else _crypto_preview_headline(snapshot)
            )
        ),
        color=discord.Color.orange(),
    )
    digits = 4 if asset == "XRP" else 2
    price = (
        f"${snapshot.price:,.{digits}f}\n"
        f"Reference candle • {snapshot.venue}"
        if current and snapshot.price is not None else "Unavailable"
    )
    expiry = (
        discord.utils.format_dt(snapshot.ends_at, style="R")
        if current else "No live window"
    )
    strength = "Unverified"
    if current and snapshot.probability is not None:
        strength = (
            f"{snapshot.probability:.1%} raw score\n"
            f"{model_strength(snapshot.probability)}"
            + (
                f" • {snapshot.training_evaluated} walk-forward examples"
                if snapshot.training_evaluated else ""
            )
        )
        if snapshot.status == "weak_model":
            below = below_line_label({
                "direction": snapshot.direction,
                "probability": snapshot.probability,
                "confidence_pct": round(snapshot.probability * 100),
            })
            if below is not None:
                strength += f"\n{below}"
        elif snapshot.status == "unqualified_model":
            strength += "\nNot a qualified call: not enough proof yet"
        lean = (
            {"probability": snapshot.live_lean_probability,
             "as_of": snapshot.live_lean_as_of}
            if snapshot.live_lean_probability is not None else None
        )
        lean_text = lean_short(lean)
        strength += (
            f"\nLive lean: {lean_text}"
            if lean_text is not None
            else "\nLive lean unavailable — data stale"
        )
    fields = (
        ("💰 Reference price", price),
        ("⏱️ Expires", expiry),
        ("⚡ Strength", strength),
        ("📊 Accuracy", ("Refresh Signal checks the same-venue 7-day "
         "settled record and its sample size")),
    )
    for index, (heading, value) in enumerate(fields):
        embed.add_field(name=heading, value=value, inline=True)
        # Match the supplied rival_ui.py: three Discord inline fields per
        # row, with a blank third tile creating two visible columns.
        if index in (1, 3):
            embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.set_footer(
        text="Reference candle ≠ live bid/ask • "
             "Raw model score ≠ measured accuracy • No orders",
    )
    return embed


class GuidedHome(discord.ui.View):
    """One private menu; real actions are wired to existing authoritative services."""

    def __init__(
        self, *, owner_id: int, location: LocationOnboardingService,
        builder: ParlayBuilder, collector: CurrentBoardCollector | None,
        refresh_sports: Callable[[], str] | None = None,
        market: PerpetualMarketStore | None, targets: DirectionTargetService,
        paper: PaperLedger, quotes: PublicBestQuoteClient,
        profit: ProspectiveQuoteProfitMonitor | None,
        research_factory: Callable[[int], discord.ui.View] | None = None,
        stream_shadow: StreamShadowLedger | None = None,
        multi: MultiExchangeResearch | None = None,
        venues: tuple[str, ...] = (),
        auto_hunt: AutoHuntService | None = None,
        training_status: Callable[[str, int], dict] | None = None,
        polymarket_status: Callable[[str, int], dict] | None = None,
        dkex_status: Callable[[], dict] | None = None,
        kalshi: KalshiCryptoBrowser | None = None,
        kalshi_status: Callable[..., dict] | None = None,
        cdna_personal: CDNAPersonalReader | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.location = location
        self.builder = builder
        self.collector = collector
        self.refresh_sports = refresh_sports
        self.market = market
        self.targets = targets
        self.auto_hunt = auto_hunt
        self.paper = paper
        self.quotes = quotes
        self.profit = profit
        self.stream_shadow = stream_shadow
        self.multi = multi
        self.venues = venues
        self.research_factory = research_factory
        self.training_status = training_status
        self.polymarket_status = polymarket_status
        self.dkex_status = dkex_status
        self.kalshi = kalshi
        self.kalshi_status = kalshi_status
        self.cdna_personal = cdna_personal
        self._kalshi_refresh = False
        self.live_source: str | None = None
        self.page = "home"
        self.back_stack: list[str] = []
        self.coin = "BTC"
        self.horizon = 5
        self.risk = RiskMode.BALANCED
        self.slip = None
        self.note: str | None = None
        self._redraw_lock = asyncio.Lock()
        # Shared across rendered generations to prevent a slow old forecast
        # from overwriting a newer coin selection or Home navigation.
        self._render_revision = {"value": 0}
        self._live_completion = {"task": None}
        self._buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "That is a private menu. Use /rival to start your own.", ephemeral=True,
        )
        return False

    def _add(self, title: str, action: str, row: int, emoji: str | None = None,
             primary: bool = False) -> None:
        self.add_item(_Button(title, action, row, emoji, primary))

    def _buttons(self) -> None:
        self.clear_items()
        p = self.page
        if p == "home":
            for title, action, row, emoji in (
                ("Crypto", "crypto-desk", 0, "📈"),
                ("Sports", "sports", 0, "🏆"),
                ("Notifications", "hunt", 1, "🔔"),
                ("Bot Performance", "performance", 1, "📊"),
                ("Practice", "practice", 2, "💵"),
                ("Live Panel", "live-poly", 2, "📡"),
            ):
                self._add(title, action, row, emoji, action == "live")
            self._add("Help", "help", 3, "❓")
        elif p == "discover":
            self._add("Crypto", "crypto-desk", 0, "📈", True)
            self._add("Practice", "practice", 0, "💵")
        elif p == "crypto-desk":
            self.add_item(_Market(self.live_source))
            self.add_item(_Coin(self.coin, row=1))
            if self.live_source != "kalshi":
                self.add_item(_PolymarketHorizon(
                    self.horizon, row=2, market=self.live_source or "polymarket",
                ))
            if self.live_source == "polymarket":
                self._add("View Contract Call", "desk-call", 3, "🎯", True)
                self._add("Track Market", "desk-track", 3, "🔔")
                self._add("Live Panel", "live-poly", 4, "📡")
            elif self.live_source == "exchange":
                self._add("Get Exchange Forecast", "desk-call", 3, "🎯", True)
                self._add("Follow Forecast", "desk-track", 3, "🔔")
            elif self.live_source == "kalshi":
                self._add("Check Kalshi Contracts", "desk-call", 3, "🔎", True)
            elif self.live_source == "draftkings":
                self._add("Find My Coin", "cdna-auto", 3, "🔎", True)
                self._add("Check CDNA Quote", "cdna-quote", 3, "💱")
                self._add("Independent AI", "dk-independent-ai", 3, "🧠")
                self.add_item(discord.ui.Button(
                    label="Open DraftKings Predictions",
                    url="https://predictions.draftkings.com/en",
                    row=4,
                ))
        elif p == "sports":
            self._add("Choose State", "state", 0, "🗺️")
            self._add("Enter ZIP", "zip", 0, "📍")
            self._add("Refresh Odds", "refresh", 0, "🔄")
            self._add("Choose My Picks", "manual", 1, "🎟️", True)
            self._add("Auto-Suggest 3", "slip", 1, "🎲")
        elif p == "slip":
            self._add("Lower Risk", "lower", 0, "🛡️")
            self._add("Balanced", "balanced", 0, "⚖️")
            self._add("Higher Payout", "aggressive", 0, "🎯")
            self._add("Try Again", "slip", 1, "🔄")
        elif p in ("crypto", "forecast", "quote"):
            self.add_item(_Coin(self.coin))
            self.add_item(_Horizon(self.horizon))
            self._add("Refresh Forecast" if p == "forecast" else "Get Signal",
                      "forecast", 2, "🔄" if p == "forecast" else "🎯", True)
            self._add("Watch Forecast", "follow", 2, "🔔")
            self._add("Market & Accuracy", "crypto-desk", 3, "📊")
        elif p == "live":
            self.add_item(_Coin(self.coin))
            if self.live_source == "polymarket":
                self.add_item(_PolymarketHorizon(self.horizon))
                self._add("Show DM Panel", "poly-start", 2, "📡", True)
                self._add("Stop Tracking", "poly-stop", 2, "🔕")
                self._add("Refresh", "live", 2, "🔄")
            elif self.live_source == "kalshi":
                self._add("Refresh Kalshi", "kalshi-refresh", 2, "🔄", True)
            self._add("Analysis & Markets", "crypto-desk", 3, "📊")
        elif p == "hunt":
            self._add("Choose Market", "crypto-desk", 0, "📈", True)
            self._add("Exchange Forecast", "crypto", 0, "🎯")
            self._add("Stop All", "stop-all", 1, "🛑")
        elif p == "kalshi":
            self._add("Analysis & Markets", "crypto-desk", 0, "📊", True)
        elif p == "hunt-tools":
            self._add("Notifications", "hunt", 0, "🔔", True)
        elif p == "practice":
            self.add_item(_Coin(self.coin))
            self._add("Practice Buy $25", "buy", 1, "📈", True)
            self._add("Practice Sell $25", "sell", 1, "📉")
            self._add("Close Fake Trade", "close", 2, "✅")
            self._add("Set Fake Balance", "funds", 2, "💵")
            self._add("My Practice Results", "results", 3, "📊")
        elif p == "results":
            self._add("Back to Practice", "practice", 0, "💵", True)
        elif p == "performance":
            self.add_item(_Coin(self.coin, row=0))
            self.add_item(_Horizon(self.horizon, row=1))
            self._add("Refresh", "performance", 2, "🔄", True)
            if self.research_factory is not None:
                self._add("Detailed Research", "research", 2, "🔬")
                self._add("Track Next Predictions", "track-forward", 3, "🎯")
            self._add("After-Cost Check", "proof", 3, "💸")
        elif p == "help":
            self._add("Start with Sports", "sports", 0, "🏆")
            self._add("Analyze Crypto", "crypto-desk", 0, "📈")
            self._add("Practice Safely", "practice", 1, "💵")
            # Exact-price targets, technical research and API limits live here
            # now that their separate /rival-tools commands are gone.
            self._add("Advanced Tools", "advanced", 2, "⚙️")
        elif p == "advanced":
            self._add("Bot Performance", "performance", 0, "📊", True)
            self._add("Technical Research", "research", 1, "🧪")
            self._add("API Usage & Limits", "api-limits", 2, "🔒")
            self._add("Manual Price Target", "target", 2, "🎯")
        if p != "home":
            self._add("Home", "home", 4, "🏠")
            self._add("Back", "back", 4, "↩️")

    def navigate(self, page: str) -> None:
        if page in ("feeds", "trust"):
            page = "performance"  # Legacy buttons use the single scorecard.
        if page == self.page:
            return
        if page == "home":
            self.back_stack.clear()
        else:
            self.back_stack.append(self.page)
            self.back_stack = self.back_stack[-12:]
        self.page = page
        if page in ("live", "crypto-desk") and self.horizon not in (5, 15, 60):
            self.horizon = 5
        self.note = None

    async def _refresh_odds(self) -> str:
        if self.refresh_sports is not None:
            return self.refresh_sports()
        return "Automatic sportsbook refresh unavailable. No new odds confirmed."

    async def render(self) -> discord.Embed:
        p = self.page
        if p == "home":
            embed = discord.Embed(
                title="RivaL | Betting Genie",
                description=(
                    "Look at crypto and sports markets, see how often the bot's "
                    "predictions were right, or practice with pretend money.\n"
                    "Only you can see this. No real bets or trades are ever placed."
                ), color=discord.Color.blurple(),
            )
        elif p == "crypto-desk":
            market = self.live_source
            selected = MARKET_NAMES.get(market, "Not selected")
            embed = discord.Embed(
                title=f"📊 RivaL | Crypto Analysis • {self.coin}",
                description=(
                    "**Choose the platform you actually use.**\n"
                    "Event contracts and leveraged futures have different "
                    "rules, prices, and prediction results.\n"
                    f"**Selected market:** {selected}"
                ),
                color=discord.Color.blurple(),
            )
            if market is None:
                embed.add_field(
                    name="Start here",
                    value=(
                        "**Polymarket:** Crypto UP/DOWN event contracts.\n"
                        "**Crypto Perpetuals:** Long/short BTC, ETH, SOL, etc. "
                        "on exchanges like Hyperliquid, Binance Futures and OKX. "
                        "**Not DraftKings.**\n"
                        "**Kalshi:** Separate YES/NO event contracts; browse only.\n"
                        "**DraftKings Predictions:** NOT connected. Owner-only "
                        "CDNA public quote reference, and independent RivaL AI "
                        "on exchange data; no confirmed contract match.\n"
                        "**DraftKings Sportsbook:** Sports odds and championship "
                        "futures belong under **Sports**, not Crypto Perpetuals."
                    )[:1024], inline=False,
                )
            elif market == "kalshi":
                progress = (
                    "No validated Kalshi-specific RivaL model yet. "
                    "Real market outcomes are being collected where supported."
                )
                if self.kalshi_status is not None:
                    try:
                        evidence = await asyncio.wait_for(
                            asyncio.to_thread(
                                self.kalshi_status, asset=self.coin,
                            ), timeout=1.1,
                        )
                        count = evidence["candidate_forward_resolved"]
                        candidate = evidence["candidate_brier"]
                        market_baseline = evidence["kalshi_quote_baseline_brier"]
                        comparison = (
                            f"Cross-series pooled Brier (NOT eligible): {candidate:.4f}; "
                            f"pooled quote baseline: {market_baseline:.4f}"
                            if candidate is not None and market_baseline is not None
                            else "No independent model-vs-Kalshi outcome comparison yet."
                        )
                        progress = (
                            "No validated Kalshi-specific RivaL model yet.\n"
                            f"Recorded Kalshi markets: **{evidence['observed']}**\n"
                            f"Book-verified market records: **{evidence['book_verified_observed']}**\n"
                            f"Shadow weight updates (legacy may be included): **{evidence['trained_labels']}**\n"
                            f"Legacy summary-price settlements excluded: "
                            f"**{evidence['legacy_unverified_settlements']}**\n"
                            f"Book-backed forward outcomes: **{count}**\n"
                            f"{comparison}\n"
                            "Research only. Model approval requires prospective "
                            "evidence and verified after-fee advantage."
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("Kalshi forward evidence unavailable")
                        progress = (
                            "Could not read Kalshi model evidence. "
                            "This is not proof the Kalshi public feed is offline."
                        )
                embed.add_field(
                    name=f"🎯 Kalshi model validation • {self.coin}",
                    value=progress[:1024], inline=False,
                )
                embed.add_field(
                    name="Next action",
                    value="**Check Kalshi Contracts** to see matched live quotes, "
                          "and published settlement rules. Research only.",
                    inline=False,
                )
            elif market == "draftkings":
                embed.add_field(
                    name="DraftKings Predictions • NOT CONNECTED (references only)",
                    value=(
                        f"**Coin:** {self.coin} • **Exchange research window only (not contract expiry):** "
                        f"{self.horizon}m\n"
                        "**Find My Coin:** Automatically find public CDNA candidates; "
                        "CDNA and DraftKings contracts are NOT automatically the same.\n"
                        "**Check CDNA Quote:** One owner-only personal reference; "
                        "no automatic feed, saved provider data, or orders.\n"
                        "**Independent AI:** RivaL's existing free AI examines "
                        "independent exchange research for this coin, NOT "
                        "CDNA or DraftKings prices or contract outcomes.\n"
                        "**No verified DraftKings live price feed, contract-specific "
                        "trained model, or settled prediction statistics yet.** "
                        "Use DraftKings' own listed strike, settlement terms, "
                        "fees, and executable quote before a decision."
                    )[:1024], inline=False,
                )
                if self.dkex_status is not None:
                    try:
                        records = await asyncio.wait_for(
                            asyncio.to_thread(self.dkex_status), timeout=1.2,
                        )
                        count = int(records.get("daily_reports") or 0)
                        observed = int(records.get("settled_contract_rows") or 0)
                        evidence = (
                            f"**{count}** locally imported DKeX daily files; "
                            f"**{observed}** settlement rows. "
                            "Source identity still needs independent verification. These are delayed reports, not live contract odds "
                            "or verified forward model wins."
                            if count else
                            "No DKeX daily files have been "
                            "imported by RivaL yet. Public daily market and "
                            "settlement reports exist; real-time actionable "
                            "contract data is still not connected."
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("DKeX research evidence read failed")
                        evidence = (
                            "Could not read local DKeX public report records. "
                            "No live DraftKings model or odds are being assumed."
                        )
                    embed.add_field(
                        name="DKeX public research evidence",
                        value=evidence, inline=False,
                    )
                embed.add_field(
                    name="Two different DraftKings products",
                    value=(
                        "**Sportsbook/Casino app:** Sports wagers and "
                        "long-term sports futures.\n"
                        "**DraftKings Predictions:** A separate event-contract "
                        "trading product with its own wallet. Neither is "
                        "a Binance/Hyperliquid-style crypto perpetual."
                    ), inline=False,
                )
            elif market == "exchange":
                embed.add_field(
                    name="Crypto perpetual futures",
                    value=(
                        "These are exchange-price forecasts for leveraged futures, "
                        "**not** matched Polymarket, Kalshi, or DraftKings contracts.\n"
                        "**Get Exchange Forecast** requests a live research signal; "
                        "**Follow Forecast** tracks one forecast.\n"
                        "Use **Home → Bot Performance → Crypto Perpetuals** "
                        "for the actual forward score, denominator, and learning record."
                    ),
                    inline=False,
                )
            elif market == "polymarket":
                embed.add_field(
                    name="Exact Polymarket contracts",
                    value=(
                        "**View Contract Call** checks the currently listed "
                        "Up/Down contract and its genuine quote.\n"
                        "**Track Market** updates one DM when meaningful changes happen.\n"
                        "Use **Home → Bot Performance → Polymarket** for "
                        "settled contract forecasts, model scores, and "
                        "how many outcomes the model has learned from."
                    ),
                    inline=False,
                )
        elif p == "discover":
            embed = discord.Embed(
                title="RivaL | Start Here",
                description="Open Crypto for verified markets or Practice for fake trades.",
                color=discord.Color.blurple(),
            )
        elif p in ("live", "kalshi"):
            source = self.live_source if p == "live" else "kalshi"
            if source == "polymarket":
                service = getattr(
                    getattr(self.auto_hunt, "bot", None),
                    "polymarket_board", None,
                )
                tracker = None
                if service is not None:
                    try:
                        tracker = await asyncio.wait_for(
                            asyncio.to_thread(service.store.get, str(self.owner_id)),
                            timeout=0.7,
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.info("Tracker status unavailable")
                if tracker is not None:
                    tracking = (
                        "ON • A single live DM panel updates this market"
                        if tracker.asset == self.coin and tracker.horizon == self.horizon
                        else f"ON • Tracking {tracker.asset} {tracker.horizon}m instead"
                    )
                    channel_id = str(getattr(tracker, "channel_id", ""))
                    message_id = str(getattr(tracker, "message_id", ""))
                    if channel_id.isdecimal() and message_id.isdecimal():
                        tracking += (
                            "\n[Open your live DM panel]"
                            f"(https://discord.com/channels/@me/{channel_id}/{message_id})"
                        )
                    tracking += "\nUse Show DM Panel to bring it to the bottom of your chat."
                else:
                    tracking = "OFF • Use Show DM Panel to create your updating card."
                cached = None
                if service is not None and callable(getattr(service, "preview", None)):
                    try:
                        cached = await service.preview(self.coin, self.horizon)
                    except Exception:  # noqa: BLE001
                        _LOG.info("Exact Polymarket preview unavailable")
                board, call = cached if cached is not None else (None, None)
                lookup = getattr(service, "_league_entry", None)
                entry = (lookup(self.coin, self.horizon, board.current_quote if board else None)
                         if callable(lookup) else None)
                formula = getattr(service, "formula", None)
                reading = (formula.latest.get(board.current.slug)
                           if formula is not None and board is not None and board.current is not None
                           else None)
                embed = live_polymarket_embed(
                    board, call, asset=self.coin, horizon=self.horizon,
                    league=entry, formula=reading,
                )
                embed.add_field(name="🔔 Your tracking", value=tracking, inline=False)
            elif source == "kalshi":
                embed = discord.Embed(
                    title=f"🔎 Kalshi | {self.coin}",
                    description=(
                        "**Live Kalshi contract analysis.** Only matched Kalshi "
                        "prices and frozen research are shown. No approved "
                        "Kalshi model or executable trade recommendation."
                    ),
                    color=discord.Color.orange(),
                )
                if self.kalshi is None:
                    embed.add_field(name="Verified markets", value="Kalshi browser unavailable.")
                else:
                    try:
                        found = await asyncio.wait_for(
                            self.kalshi.browse(
                                self.coin, refresh=self._kalshi_refresh,
                            ), timeout=3.5,
                        )
                        self._kalshi_refresh = False
                        if not found.markets:
                            embed.add_field(
                                name="Verified markets",
                                value="No matching open contract. No Polymarket substitution.",
                                inline=False,
                            )
                        for item in found.markets[:2]:
                            yes = f"USD {item.yes_ask:.2f}" if item.yes_ask is not None else "unverified"
                            no = f"USD {item.no_ask:.2f}" if item.no_ask is not None else "unverified"
                            quote_context = (
                                "Executable depth not verified. No entry-price "
                                "or prediction evidence may be inferred."
                            )
                            if (
                                item.quote_source == "orderbook"
                                and item.yes_ask is not None
                                and item.no_ask is not None
                            ):
                                both = item.yes_ask + item.no_ask
                                quote_context = (
                                    f"For a standard $1 payout, YES needs >{item.yes_ask:.0%} wins; "
                                    f"NO needs >{item.no_ask:.0%} wins to break "
                                    f"even before fees. Both asks total ${both:.2f}. "
                                    "These are price thresholds, NOT forecasts. "
                                    f"Top available contracts: YES {item.yes_ask_size:g}; "
                                    f"NO {item.no_ask_size:g}. "
                                    "Quotes can change before an actual fill."
                                )
                            research_context = (
                                "No independently validated Kalshi forecast for "
                                "this contract. Historical summary quotes are "
                                "not accepted as executable book evidence."
                            )
                            try:
                                shadow = await asyncio.wait_for(
                                    self.kalshi.contract_research(
                                        asset=self.coin, market=item,
                                        observed_at=found.checked_at,
                                    ),
                                    timeout=0.9,
                                )
                                if shadow and shadow["candidate_yes"] is not None:
                                    research_context = (
                                        "EXPERIMENTAL shadow P(YES): "
                                        f"{shadow['candidate_yes']:.1%} "
                                        f"(based on {shadow['candidate_training_labels']} "
                                        "previous settled labels in this exact "
                                        "series). NOT validated or approved; "
                                        "not a trading call."
                                    )
                            except (TimeoutError, ValueError, TypeError, RuntimeError):
                                research_context = (
                                    "Kalshi research ledger unavailable; "
                                    "no forecast was substituted."
                                )
                            embed.add_field(
                                name=item.title[:140],
                                value=(
                                    f"{item.ticker} • closes "
                                    f"{discord.utils.format_dt(item.close_time, style='R')}\n"
                                    f"YES ask: {yes} • NO ask: {no}\n"
                                    f"{quote_context}\n"
                                    f"{research_context}\n"
                                    f"Rule: {item.rules[:140]}"
                                )[:1024], inline=False,
                            )
                    except (TimeoutError, ValueError, TypeError):
                        embed.add_field(
                            name="Verified markets", value="Kalshi feed unavailable.",
                            inline=False,
                        )
            else:
                embed = discord.Embed(
                    title="Choose Your Market",
                    description="Select a market in Crypto Analysis before requesting a call.",
                    color=discord.Color.orange(),
                )
        elif p in ("hunt", "hunt-tools"):
            tracker = getattr(getattr(self.auto_hunt, "bot", None),
                              "polymarket_board", None)

            async def current(read):
                if read is None:
                    return None
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(read, str(self.owner_id)), timeout=0.9,
                    )
                except Exception:  # noqa: BLE001
                    return "unknown"

            market = await current(tracker.store.get if tracker else None)
            exchange = await current(self.targets.store.get if self.targets else None)
            prefs = await current(
                self.auto_hunt.store.preferences if self.auto_hunt else None
            )
            legacy = (
                "Legacy price-shift alerts have been retired."
                if prefs is not None else
                "Old alert preferences unavailable."
            )

            def status(value):
                if value == "unknown":
                    return "Checking…"
                return "ON" if value is not None else "OFF"

            embed = discord.Embed(
                title="🔔 Notifications",
                description=(
                    f"**Polymarket tracking:** {status(market)}\n"
                    f"**Exchange forecast watch:** {status(exchange)}\n"
                    f"{legacy}\n\n"
                    "Tracking updates one private message. No price-change "
                    "or Auto-Hunt notification spam. Background model training "
                    "does not DM you. **Stop All** cancels your personal updates."
                ),
                color=discord.Color.blurple(),
            )
        elif p == "sports":
            # Browsing the sports menu is not a bet recommendation. In
            # particular, do not run the expensive positive-edge builder here.
            embed = discord.Embed(
                title="🏆 Sports & Odds",
                description=(
                    "**Browse games and compare sportsbook prices.**\n"
                    "Choose My Picks to browse available selections.\n"
                    "Auto-Suggest 3 checks for qualifying recommendations; "
                    "it may return PASS when no positive edge is verified.\n\n"
                    "Odds refresh in the background. No bet is placed."
                ),
                color=discord.Color.blurple(),
            )
            try:
                saved = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.location.get_saved, platform="discord",
                        user_id=str(self.owner_id),
                    ),
                    timeout=2,
                )
                embed.add_field(
                    name="Your State",
                    value=saved.state_name if saved is not None else
                          "Not set • Choose State or Enter ZIP",
                    inline=False,
                )
            except TimeoutError:
                embed.add_field(
                    name="Your State",
                    value="Checking saved state • Choose State if needed",
                    inline=False,
                )
            embed.add_field(
                name="Model research scope",
                value=SPORTS_MODEL_SCOPE_TEXT,
                inline=False,
            )
            if self.collector is not None:
                try:
                    last = await asyncio.wait_for(
                        asyncio.to_thread(self.collector._last_refresh),
                        timeout=2,
                    )
                    updating = self.collector._lock.locked()
                    if last is None:
                        status = ("Updating odds in background" if updating
                                  else "No completed odds refresh yet")
                    else:
                        age = max(0, int((datetime.now(UTC) - last).total_seconds() // 60))
                        status = f"Last checked {age} min ago"
                        if updating:
                            status += " • Updating in background"
                        elif age >= int(self.collector.refresh_interval.total_seconds() // 60):
                            status += " • Update due"
                    embed.add_field(name="Odds status", value=status, inline=False)
                except TimeoutError:
                    embed.add_field(
                        name="Odds status",
                        value="Checking update status in background",
                        inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Sports refresh status unavailable")
                    embed.add_field(
                        name="Odds status", value="Refresh status unavailable",
                        inline=False,
                    )
                notice_getter = getattr(self.collector, "_last_notice", None)
                if callable(notice_getter):
                    try:
                        notice = await asyncio.wait_for(
                            asyncio.to_thread(notice_getter),
                            timeout=2,
                        )
                    except TimeoutError:
                        notice = None
                    except Exception:  # noqa: BLE001
                        _LOG.exception("Sports pause notice unavailable")
                        notice = None
                    if notice:
                        embed.add_field(
                            name="Sports collection paused",
                            value=str(notice)[:200],
                            inline=False,
                        )
        elif p == "slip":
            if self.slip is None:
                embed = discord.Embed(
                    title="🎟️ Sports Slip",
                    description="PASS: No qualifying available prices. No wager made.",
                    color=discord.Color.orange(),
                )
                diagnostics = (
                    getattr(self.builder, "last_diagnostics", None)
                    if self.builder is not None else None
                )
                if diagnostics:
                    reason = _parlay_diagnostics_text(diagnostics)
                    if reason:
                        embed.add_field(
                            name="Why PASS", value=reason, inline=False,
                        )
            else:
                embed = parlay_embed(self.slip)
                embed.title = "🎟️ Your Sports Slip"
                embed.description = (
                    (embed.description or "")
                    + "\n\n**No wager was placed. All legs must win.**"
                )
        elif p == "crypto":
            # Loading the crypto menu must never wait for a contended
            # PostgreSQL pool or a full research query. Get Signal performs
            # the authoritative refresh separately, on demand.
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.to_thread(
                        target_snapshot, self.market,
                        asset=self.coin, horizon_minutes=self.horizon,
                    ),
                    timeout=1.5,
                )
            except (TimeoutError, Exception):  # noqa: BLE001 - keep navigation alive
                _LOG.warning("RivaL crypto menu preview unavailable (non-blocking)")
                snapshot = None
            embed = crypto_snapshot_card(
                snapshot, asset=self.coin, horizon=self.horizon,
            )
        elif p == "forecast":
            try:
                # The render itself is capped at 12s; leave headroom so a
                # slow-but-successful signal still delivers instead of
                # timing out into SIGNAL DELAYED.
                embed = await asyncio.wait_for(
                    signal_embed(
                        self.market, self.quotes,
                        asset=self.coin, horizon_minutes=self.horizon,
                        stream_shadow=self.stream_shadow,
                    ),
                    timeout=10,
                )
            except TimeoutError:
                embed = discord.Embed(
                    title=f"⏳ {self.coin} | {self.horizon}m • SIGNAL DELAYED",
                    description=(
                        "The market check is taking too long. No fresh signal "
                        "was verified, and the previous forecast is not a new trade. "
                        "Try again shortly."
                    ),
                    color=discord.Color.orange(),
                )
        elif p == "quote":
            try:
                price = await self.quotes.get(self.coin)
            except QuoteUnavailable:
                reference = await self.quotes.get_reference(self.coin)
                embed = discord.Embed(
                    title=f"💱 {self.coin} | Live Perpetual Reference",
                    description=(
                        "Binance-USDT executable quotes are unavailable. "
                        "The independently verified Hyperliquid USD-perp "
                        "reference below is a DIFFERENT market. It cannot "
                        "authorize a Binance practice trade or profit proof."
                    ),
                    color=discord.Color.orange(),
                )
                embed.add_field(
                    name="Hyperliquid USD-perp top of book",
                    value=(
                        f"Bid **${reference.bid:,.2f}** • "
                        f"Ask **${reference.ask:,.2f}** · "
                        f"Exchange update "
                        f"{discord.utils.format_dt(reference.exchange_at, style='R')} "
                        "· Reference only, no trade"
                    ),
                    inline=False,
                )
            else:
                embed = quote_embed(price, quantity=0.001)
        elif p in ("practice", "results"):
            stats = await asyncio.to_thread(self.paper.summary, str(self.owner_id))
            money = stats["practice"]
            embed = discord.Embed(
                title=("💵 My Practice Money" if p == "practice" else "💵 My Practice Results"),
                description=(
                    "**100% imaginary funds.** Practice Buy expects prices to "
                    "rise; Practice Sell expects a drop. Neither submits an order."
                    if p == "practice" else
                    "Only your personal imaginary trades appear here. "
                    "See Bot Performance for the bot's actual predictions."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="My Fake Balance",
                value=f"**{money['equity']:,.2f} practice USDT**",
                inline=False,
            )
            embed.add_field(
                name="Available / Reserved",
                value=f"{money['available']:,.2f} / {money['committed']:,.2f} USDT",
                inline=False,
            )
            embed.add_field(
                name="My Practice Results",
                value=(
                    f"{stats['open']} open, {money['completed']} finished\n"
                    f"{money['wins']} wins, {money['losses']} losses\n"
                    f"Estimated net: {money['realized']:+,.2f} USDT"
                ), inline=False,
            )
            if p == "practice":
                embed.add_field(
                    name="Daily Safety Limit",
                    value=(
                        f"{money['day_loss']:,.2f} of "
                        f"{money['daily_loss_cap']:,.2f} practice USDT used"
                    ), inline=False,
                )
            else:
                embed.add_field(
                    name="Practice is not performance",
                    value="These are fictional balances and trades, not RivaL's forecast "
                          "accuracy or actual profit. Open **Bot Performance** from Home "
                          "to inspect scored predictions.",
                    inline=False,
                )
        elif p == "performance":
            embed = discord.Embed(
                title=f"📊 How RivaL is doing: {self.coin}, {self.horizon}-min predictions",
                description=(
                    "**Does it make money?** Not proven yet. Every score here comes from "
                    "predictions saved *before* the result was known, so nothing can be "
                    "edited afterwards. Your own practice trades are under Practice."
                ),
                color=discord.Color.blurple(),
            )
            # The on-demand model registry / Drive / promotion report is expensive:
            # it belongs in Detailed Research, not in the interactive scorecard.
            # A slow improvement query must never suppress baseline accuracy or health.
            readers = {}
            if self.market is not None:
                readers["exchange"] = asyncio.wait_for(
                    asyncio.to_thread(_performance_exchange, self.market, self.coin),
                    timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["benefit"] = asyncio.wait_for(
                    asyncio.to_thread(
                        _cached_benefit, self.market, self.coin, self.horizon,
                    ), timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["pooled"] = asyncio.wait_for(
                    asyncio.to_thread(_pooled_record, self.market, self.coin, self.horizon),
                    timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["regime"] = asyncio.wait_for(
                    asyncio.to_thread(_regime_record, self.market, self.coin, self.horizon),
                    timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["regime_live"] = asyncio.wait_for(
                    asyncio.to_thread(_regime_live_record, self.market, self.coin, self.horizon),
                    timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["regime_live_fast"] = asyncio.wait_for(
                    asyncio.to_thread(_regime_live_fast_record, self.market, self.coin, self.horizon),
                    timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["intelligence"] = asyncio.wait_for(
                    asyncio.to_thread(
                        _intelligence_status, self.market, self.coin, self.horizon,
                    ), timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["coverage"] = asyncio.wait_for(
                    asyncio.to_thread(_cycle_coverage, self.market), timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["lineage"] = asyncio.wait_for(
                    asyncio.to_thread(
                        _performance_lineage, self.market, self.coin, self.horizon,
                    ), timeout=PERFORMANCE_READ_SECONDS,
                )
                readers["cycles"] = asyncio.wait_for(
                    asyncio.to_thread(_performance_cycles, self.market), timeout=PERFORMANCE_READ_SECONDS,
                )
                if callable(getattr(self.market, "feed_health", None)):
                    readers["feeds"] = asyncio.wait_for(
                        asyncio.to_thread(self.market.feed_health), timeout=PERFORMANCE_READ_SECONDS,
                    )
            if self.polymarket_status is not None:
                readers["polymarket"] = asyncio.wait_for(
                    asyncio.to_thread(
                        self.polymarket_status, self.coin, self.horizon,
                    ), timeout=PERFORMANCE_READ_SECONDS,
                )
            reports = dict(zip(
                readers, await asyncio.gather(*readers.values(), return_exceptions=True),
            ))
            for name, result in reports.items():
                if isinstance(result, Exception):
                    _LOG.warning("Performance %s read failed: %s", name, type(result).__name__)
            now = datetime.now(UTC)

            # 1. Actual seven-day exchange scores, independent from trend queries.
            exchange = reports.get("exchange")
            part = (
                exchange.get("last_7d", {}).get(self.horizon)
                if isinstance(exchange, dict) else None
            )
            if not isinstance(part, dict):
                exchange_text = ("Couldn't load the scores right now (that doesn't mean "
                                 "zero wins). Press Refresh.")
            else:
                decided = int(part.get("decided") or 0)
                correct = int(part.get("correct") or 0)
                settled = int(part.get("settled") or 0)
                issued = int(part.get("issued") or 0)
                hit = part.get("hit_rate")
                accuracy = (
                    f"**{correct:,} of {decided:,}** ({hit:.1%})"
                    if isinstance(hit, (int, float)) and decided else
                    "not enough finished results to measure yet"
                )
                brier, baseline = part.get("brier"), part.get("baseline_brier")
                if isinstance(brier, (int, float)) and isinstance(baseline, (int, float)):
                    verdict = ("Slightly, so far, but not proven enough to trade on"
                               if brier < baseline else "No, not yet")
                    scoring = (f"**Better than a simple guess?** {verdict}. "
                               f"(Error score: RivaL {brier:.3f}, simple guess "
                               f"{baseline:.3f}; lower is better. The simple guess "
                               "always uses how often the price usually rises.)")
                else:
                    scoring = "**Better than a simple guess?** Too early to compare fairly"
                # Overdue results are the only preamble health signal kept:
                # a late result is a real problem, while the waiting
                # count, the unscored breakdown, and the "no results
                # running late" line are routine. The timing paragraph
                # is cut too; the coverage (scope + period) joins the
                # Checked line, so the preamble is one short line and
                # the 1024-char field budget goes to the trial rows.
                # The honesty disclosures live in the embed description
                # ("Does it make money? Not proven yet", "predictions
                # saved before the result was known"), not in this field,
                # and stay.
                overdue_line = ""
                if "due" in part:
                    overdue = int(part.get("overdue") or 0)
                    if overdue:
                        overdue_line = (
                            f"\n⚠️ **{overdue:,} should have a result by now but don't** "
                            "(the candle is missing or checking is behind)")
                venues = int(part.get("venues") or 0)
                windows = int(part.get("windows") or 0)
                scope = (
                    f"{venues} exchange{'s' if venues != 1 else ''} × "
                    f"{windows:,} candles"
                    if venues and windows else "every exchange it collects"
                )
                period = "the last 7 days"
                try:
                    first = datetime.fromisoformat(str(part.get("first_issued")))
                    if first.tzinfo is not None and now - first < timedelta(days=6, hours=12):
                        days = max(1, round((now - first).total_seconds() / 86400))
                        period = (f"since this model started {first:%b %d} "
                                  f"(~{days} day{'s' if days != 1 else ''})")
                except (TypeError, ValueError):
                    pass
                problems = score_consistency_problems(part)
                if problems:
                    _LOG.error("Performance consistency check failed for %s %s min: %s",
                               self.coin, self.horizon, "; ".join(problems))
                exchange_text = (
                    f"**Called the direction right:** {accuracy}\n{scoring}\n"
                    f"**Checked:** {settled:,} of {issued:,} predictions · "
                    f"{scope}, {period}, current model only"
                    f"{overdue_line}"
                    + ("\n⚠️ **These numbers don't add up** ("
                       + "; ".join(problems)[:300]
                       + "). Don't trust them until this is fixed." if problems else "")
                )
            # Trial rows: one line per family, same format everywhere
            # (see _trial_one_liner). regime_v2 is dead — never issued
            # once, flag off — so it has no reader and no row here.
            pooled = reports.get("pooled")
            if isinstance(pooled, dict) and int(pooled.get("issued") or 0):
                exchange_text += "\n" + _trial_one_liner(
                    "Pooled 6-year model", pooled, now)
            regime = reports.get("regime")
            if isinstance(regime, dict) and int(regime.get("issued") or 0):
                exchange_text += "\n" + _trial_one_liner("regime_v1", regime, now)
            embed.add_field(name=f"📈 {self.coin} exchange predictions (up or down?)",
                            value=exchange_text[:1024], inline=False)

            # Regime live-learning rows get their own fields, one per
            # family, directly after the exchange field so the reading
            # order is unchanged. Appended last to the 1024-char exchange
            # field they were cut mid-header on 2026-11-09 (Felix's
            # Performance screenshot: the base row was cut at ~1015 chars
            # and the fast row was cut entirely). One field per family
            # keeps each one-line row (headline + detail line, all D7
            # extras) under the 1024-char field cap with room to spare
            # — a worst-case full-form row measures 356 chars, two
            # combined 712 — so no row is ever truncated and no slice
            # is applied here. _regime_live_text
            # returns "" before first issuance, and Discord rejects empty
            # field values, so a family with no text gets no field at all.
            live_text = _regime_live_text(
                reports.get("regime_live"), "Regime live-learning model", now)
            if live_text:
                embed.add_field(name="🔬 Regime live-learning model",
                                value=live_text.strip(), inline=False)
            live_fast_text = _regime_live_text(
                reports.get("regime_live_fast"), "Regime live-learning fast model", now)
            if live_fast_text:
                embed.add_field(name="🔬 Regime live-learning fast model",
                                value=live_fast_text.strip(), inline=False)

            # 2. Contract-native comparisons; historical labels are NOT model wins.
            poly = reports.get("polymarket")
            def money(cents: float) -> str:
                return (f"made {cents:.1f}¢" if cents > 0 else
                        f"lost {-cents:.1f}¢" if cents < 0 else "broke even") + " per $1"

            if not isinstance(poly, dict):
                poly_text = ("Couldn't load these scores right now (that doesn't mean "
                             "zero accuracy). Press Refresh.")
            else:
                resolved = int(poly.get("settled_forward_contracts") or 0)
                learned = int(poly.get("learned_contracts") or 0)
                window = int(poly.get("score_window_days") or 7)
                model_error, market_error = (
                    poly.get("forward_brier"), poly.get("market_baseline_brier"),
                )
                if (resolved and isinstance(model_error, (int, float))
                        and isinstance(market_error, (int, float))):
                    outcome = (
                        "Slightly, so far, but not proven enough to trade on"
                        if model_error < market_error else "No, not yet"
                    )
                    poly_text = (
                        f"**Checked:** {resolved:,} live predictions (last {window} days)\n"
                        f"**Better than Polymarket's own odds?** {outcome}. "
                        f"(Error score: RivaL {model_error:.3f}, Polymarket "
                        f"{market_error:.3f}; lower is better.)"
                    )
                else:
                    poly_text = (f"**Checked:** {resolved:,} live predictions "
                                 "· too early to compare with Polymarket's odds")
                observed = int(poly.get("observed_contracts") or 0)
                pooled = poly.get("pooled") or {}
                if pooled.get("contracts"):
                    verdict = ("better" if pooled["model_brier"] < pooled["market_brier"]
                               else "not better")
                    poly_text += (
                        f"\n**New 6-year model vs Polymarket's odds:** {verdict} on "
                        f"{pooled['contracts']:,} contracts ({pooled['model_brier']:.3f} vs "
                        f"{pooled['market_brier']:.3f})"
                    )
                    if pooled.get("real_value_bets"):
                        poly_text += (
                            f". When it disagreed with the odds, betting its side would "
                            f"have won {pooled['real_value_bet_wins']} of "
                            f"{pooled['real_value_bets']} and "
                            f"{money(pooled['real_return_per_dollar_after_fees'] * 100)} "
                            "after Polymarket's fee"
                        )
                    elif pooled.get("value_bets"):
                        poly_text += (
                            f". When it disagreed with the odds by 5+ points, its side won "
                            f"{pooled['value_bet_wins']} of {pooled['value_bets']} and "
                            f"{money(pooled['value_bet_avg_return'] * 100)} before fees"
                        )
                kalshi = poly.get("kalshi") or {}
                if kalshi.get("contracts"):
                    k_verdict = ("better" if kalshi["model_brier"] < kalshi["market_brier"]
                                 else "not better")
                    poly_text += (
                        f"\n**Kalshi 15-min (also on Robinhood, Coinbase):** {k_verdict} "
                        f"than Kalshi's odds on {kalshi['contracts']:,} contracts "
                        f"({kalshi['model_brier']:.3f} vs {kalshi['market_brier']:.3f})")
                    if kalshi.get("value_bets"):
                        poly_text += (
                            f"; betting its side when it disagreed won "
                            f"{kalshi['value_bet_wins']} of {kalshi['value_bets']} and "
                            f"{money(kalshi['return_per_dollar_after_fees'] * 100)} "
                            "after Kalshi's fees")
                labels = {"flow_v1": "Order-flow test model",
                          "flow_v2": "Order-flow test model + OKX/Coinbase"}
                for family, scored in (poly.get("kalshi_challengers") or {}).items():
                    if not scored.get("contracts"):
                        continue
                    better = scored["model_brier"] < scored["market_brier"]
                    poly_text += (
                        f"\n**{labels.get(family, family)} vs Kalshi (testing only, not "
                        f"used for calls):** {'better' if better else 'not better'} on "
                        f"{scored['contracts']:,} contracts "
                        f"({scored['model_brier']:.3f} vs {scored['market_brier']:.3f})")
                    if scored.get("value_bets"):
                        poly_text += (
                            f"; its side won {scored['value_bet_wins']} of "
                            f"{scored['value_bets']} and "
                            f"{money(scored['return_per_dollar_after_fees'] * 100)} after fees")
                poly_text += (f"\n**Past results studied:** {learned:,} (history, not live "
                              "predictions)")
                if observed and not learned:
                    poly_text += (f"\n⚠️ Saw {observed:,} contracts but couldn't read any "
                                  "of their results yet")
            embed.add_field(name="🎯 Polymarket / Kalshi predictions",
                            value=poly_text[:1024], inline=False)

            # 3. Live worker state is distinct from statistical learning benefit.
            cycles = reports.get("cycles")
            heart = cycles.get("learning") if isinstance(cycles, dict) else None
            if heart is None:
                engine_text = "**Learning loop:** ⚠️ no run recorded yet"
            elif heart.get("status") != "recorded":
                engine_text = "**Learning loop:** couldn't check right now"
            else:
                age = int((now - heart["checked_at"].astimezone(UTC)).total_seconds() // 60)
                if not heart["math_ok"] or not heart["model_review_ok"]:
                    state = "❌ a step failed on the last run"
                elif not 0 <= age <= 16:
                    state = "⚠️ hasn't run recently (or the clock is off)"
                else:
                    state = "✅ running"
                engine_text = (
                    f"**Learning loop:** {state}. Last run {age} min ago (runs every "
                    f"5 min): made {heart['issued']} predictions and checked "
                    f"{heart['settled']} results, all coins and timeframes"
                )
            benefit = reports.get("benefit")
            lineage = reports.get("lineage")
            if isinstance(benefit, dict):
                reason = {
                    "supported": "Maybe: it looks better, but that isn't proven yet",
                    "not_improving": "No: it isn't beating the simple guess yet",
                    "insufficient": "Too early to tell",
                }.get(benefit.get("status"), "Can't confirm")
                days = int(benefit.get("days") or 0)
                need = ""
                if benefit.get("status") == "insufficient" and days < 7:
                    need = f"; needs 7 days of results, has {days}"
                trend_text = (
                    f"**Getting more accurate?** {reason} "
                    f"({int(benefit.get('targets') or 0):,} results over {days} "
                    f"day{'s' if days != 1 else ''}{need})"
                )
            elif isinstance(benefit, TimeoutError):
                trend_text = ("**Getting more accurate?** Still calculating; press Refresh "
                              "in a few seconds")
            else:
                trend_text = "**Getting more accurate?** Couldn't check right now"
            if isinstance(lineage, dict) and lineage.get("lineage_proven"):
                sample = int(lineage.get("forecasts") or lineage["lineage_proven"])
                unknown = int(lineage.get("lineage_unknown") or 0)
                lineage_text = (
                    f"**Built from the 6-year history?** "
                    f"{lineage['historically_seeded']} of the last "
                    f"{lineage['lineage_proven']} predictions"
                    + (f" ({unknown:,} of the latest {sample:,} have no training record)"
                       if unknown else "")
                    + ". The 6-year model is on trial separately (above)."
                )
            else:
                lineage_text = ("**Built from the 6-year history?** Can't confirm for "
                                "recent predictions")
            intelligence = reports.get("intelligence")
            if isinstance(intelligence, dict):
                rep_ = intelligence["report"]
                latest = intelligence["latest"]
                stance, direction = latest["stances"].get(self.horizon, ("—", "none"))
                followed = (
                    f"went along with the model {rep_['followed']} times "
                    f"(right {rep_['followed_hit_rate']:.0%})"
                    if rep_["followed"] and rep_["followed_hit_rate"] is not None
                    else f"went along with the model {rep_['followed']} times "
                         "(none scored yet)"
                )
                abstained = (
                    f"sat out {rep_['abstained']} (the model was right "
                    f"{rep_['model_hit_when_abstained']:.0%} of those)"
                    if rep_["abstained"] and rep_["model_hit_when_abstained"] is not None
                    else f"sat out {rep_['abstained']}"
                )
                # "now DOWN" read as "the service is down"; spell out that it is
                # a price call, and that sitting out is a choice, not an outage.
                now_call = ({"up": "says UP (price rising)",
                             "down": "says DOWN (price falling)"}.get(direction.lower(),
                                                                     direction.upper())
                            if stance == "follow" and direction != "none"
                            else "makes no call")
                # The AI's own note is technical; it stays in Detailed Research.
                intel_text = (
                    f"\n**RivaL Intelligence (an AI that reviews the model's calls):** for "
                    f"the next {self.horizon}-min candle it {now_call}. Last 7 days it "
                    f"{followed} and {abstained}."
                )
            elif isinstance(intelligence, Exception):
                intel_text = "\n**RivaL Intelligence:** couldn't check right now"
            else:
                intel_text = ("\n**RivaL Intelligence:** hasn't reviewed anything yet"
                              if "intelligence" in reports else "")
            embed.add_field(
                name="🧠 Is it learning?",
                value=f"{engine_text}\n{trend_text}\n{lineage_text}{intel_text}"[:1024],
                inline=False,
            )

            # 4. Independent operations health. A green feed is never a
            # substitute for an actual forecast-worker heartbeat.
            feeds = reports.get("feeds")
            if not isinstance(feeds, (list, tuple)):
                feeds_text = "**Price feeds:** couldn't check right now"
            else:
                fresh, listed = set(), set()
                for row in feeds:
                    if row.get("asset") != self.coin or row.get("venue") not in self.venues:
                        continue
                    listed.add(row["venue"])
                    try:
                        last_success = datetime.fromisoformat(str(row["last_success_at"]))
                        last_candle = datetime.fromisoformat(str(row["last_candle_close"]))
                        if (last_success.tzinfo is not None
                            and last_candle.tzinfo is not None
                            and row.get("status") == "ok"
                            and timedelta(0) <= now-last_success.astimezone(UTC) <= timedelta(minutes=15)
                            and timedelta(0) <= now-last_candle.astimezone(UTC) <= timedelta(minutes=15)):
                            fresh.add(row["venue"])
                    except (ValueError, TypeError, OverflowError):
                        continue
                # Count only exchanges that actually collect this coin; with no
                # rows at all every configured exchange is missing, not fine.
                expected = listed or set(self.venues)
                stale = sorted(expected - fresh)
                feeds_text = (
                    f"**Price feeds:** {'✅' if not stale else '⚠️'} {len(fresh)} of "
                    f"{len(expected)} exchanges up to date"
                    + (f" (behind: {', '.join(stale)[:200]})" if stale else "")
                    if self.venues else "**Price feeds:** not set up"
                )
            cycle = cycles.get("forecast") if isinstance(cycles, dict) else None
            if cycle is None:
                cycle_text = "**Prediction cycle:** ⚠️ no run recorded yet" if isinstance(
                    cycles, dict,
                ) else "**Prediction cycle:** couldn't check right now"
            elif cycle.get("status") != "recorded":
                cycle_text = "**Prediction cycle:** couldn't check right now"
            else:
                age = int((now - cycle["checked_at"].astimezone(UTC)).total_seconds() // 60)
                status = ("✅" if 0 <= age <= 10 else
                          "⚠️ late (or the clock is off):")
                cycle_text = (f"**Prediction cycle:** {status} last ran {age} min ago and "
                              f"made {cycle['issued']} predictions (all coins and timeframes)"
                              + (f"; {cycle['expired']} skipped for running late"
                                 if cycle.get("expired") else ""))
            coverage = reports.get("coverage")
            if isinstance(coverage, dict) and coverage["cycles"]:
                ran = min(coverage["cycles"], 288)
                missed = coverage["expired"]
                cycle_text += (
                    f"\n**Last 24 hours:** {'✅' if ran >= 274 else '⚠️'} {ran} of 288 "
                    f"cycles ran ({ran / 288:.0%})"
                    + (f"; {missed:,} predictions skipped because a cycle ran late"
                       if missed else "")
                    + ("" if ran >= 288 else
                       ". Restarts and slow cycles both count against this")
                )
            embed.add_field(
                name="📡 Is everything running?", value=f"{feeds_text}\n{cycle_text}"[:1024],
                inline=False,
            )
            embed.timestamp = now
            embed.set_footer(
                text="Live numbers · Research only, no real trades · "
                     "More detail: Detailed Research"
            )
        elif p == "help":
            embed = discord.Embed(
                title="❓ Getting Started",
                description=(
                    "1. **Crypto:** live markets and RivaL's up/down predictions.\n"
                    "2. **Sports:** choose your state and compare odds.\n"
                    "3. **Notifications:** get updates in your messages; stop anytime.\n"
                    "4. **Bot Performance:** how often the predictions were right.\n"
                    "5. **Practice:** trade pretend money and track your results.\n"
                    "**Advanced Tools:** exact-price alerts and deeper research.\n\n"
                    "**WAIT** means RivaL isn't confident enough to make a call. "
                    "No real bets are placed."
                ), color=discord.Color.blurple(),
            )
        else:
            embed = discord.Embed(
                title="⚙️ Advanced Tools",
                description="Exact-price alerts, deeper research, and how much of each "
                            "data service is being used. Accuracy and learning are on "
                            "Bot Performance.",
                color=discord.Color.blurple(),
            )
        if self.note:
            embed.add_field(name="What Happened", value=self.note[:1000],
                            inline=False)
        if p != "performance":  # Performance sets its own footer above
            embed.set_footer(text="Research only • No real trades • Predictions can be wrong")
        return embed

    def _display_generation(self) -> GuidedHome:
        """Create the replacement buttons without detaching the displayed ones.

        The old generation stays dispatchable throughout slow renders and
        until Discord successfully registers the newly edited message.
        """
        view = GuidedHome(
            owner_id=self.owner_id, location=self.location, builder=self.builder,
            collector=self.collector, refresh_sports=self.refresh_sports,
            market=self.market, targets=self.targets, paper=self.paper,
            quotes=self.quotes, profit=self.profit,
            research_factory=self.research_factory,
            stream_shadow=self.stream_shadow, multi=self.multi,
            venues=self.venues, auto_hunt=self.auto_hunt,
            training_status=self.training_status,
            polymarket_status=self.polymarket_status,
            dkex_status=self.dkex_status,
            kalshi=self.kalshi,
            kalshi_status=self.kalshi_status,
            cdna_personal=self.cdna_personal,
        )
        view.page = self.page
        view.back_stack = list(self.back_stack)
        view.coin = self.coin
        view.horizon = self.horizon
        view.risk = self.risk
        view.slip = self.slip
        view.note = self.note
        view._kalshi_refresh = self._kalshi_refresh
        view.live_source = self.live_source
        view._redraw_lock = self._redraw_lock
        view._render_revision = self._render_revision
        view._live_completion = self._live_completion
        view._buttons()  # only the new, never-registered view is cleared
        return view

    async def redraw(self, interaction: discord.Interaction) -> None:
        old_completion = self._live_completion["task"]
        if old_completion is not None and not old_completion.done():
            old_completion.cancel()
        async with self._redraw_lock:
            self._render_revision["value"] += 1
            revision = self._render_revision["value"]
            # Acknowledge a real Discord request quickly with a truthful
            # loading view. Slow market checks never hold the button handler
            # or block navigation; only the last generation can replace it.
            if self.page in ("forecast", "performance") and isinstance(
                interaction, discord.Interaction,
            ):
                if self.page == "forecast":
                    title = f"🔄 Checking {self.coin} {self.horizon}m"
                    description = (
                        "Checking newly issued model forecasts, matching live "
                        "quotes, and real forward evidence. The previous "
                        "forecast is not automatically a new trade."
                    )
                else:
                    title = f"🔄 Loading {self.coin} performance"
                    description = (
                        "Checking saved prediction results, learning progress "
                        "and live feed health. No verified trading edge."
                    )
                loading = discord.Embed(
                    title=title, description=description,
                    color=discord.Color.blurple(),
                )
                try:
                    await asyncio.wait_for(
                        interaction.edit_original_response(
                            embed=loading, view=self._display_generation(),
                        ), timeout=4,
                    )
                except (TimeoutError, discord.HTTPException):
                    _LOG.warning("Fast forecast loading edit failed")
                    return
                _spawn(
                    self._finish_forecast(interaction, revision)
                    if self.page == "forecast" else
                    self._finish_performance(interaction, revision),
                    name="rival-forecast-details" if self.page == "forecast"
                    else "rival-performance-details",
                )
                return
            await self._redraw_locked(interaction)
            if self.page == "live" and self.live_source == "polymarket":
                service = getattr(getattr(self.auto_hunt, "bot", None), "polymarket_board", None)
                if callable(getattr(service, "wait_ready", None)):
                    self._live_completion["task"] = asyncio.create_task(
                        self._finish_live_preview(interaction, revision, service),
                        name="rival-live-market-details",
                    )

    async def _finish_live_preview(self, interaction, revision: int, service) -> None:
        end = time.monotonic() + 14 * 60  # Discord interaction tokens last 15 min
        previous = None
        try:
            while time.monotonic() < end:
                await service.wait_ready(self.coin, self.horizon)
                async with self._redraw_lock:
                    if self._render_revision["value"] != revision:
                        return
                    embed = await asyncio.wait_for(self.render(), timeout=3)
                    fingerprint = str(embed.to_dict())
                    if fingerprint != previous:
                        await asyncio.wait_for(interaction.edit_original_response(
                            embed=embed, view=self._display_generation(),
                        ), timeout=5)
                        previous = fingerprint
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a closed menu cannot stop source collection
            _LOG.info("Live market completion unavailable; shared tracking continues")

    async def _finish_performance(
        self, interaction: discord.Interaction, revision: int,
    ) -> None:
        """Complete slow scorecard reads after the user can already navigate."""
        try:
            embed = await asyncio.wait_for(self.render(), timeout=8)
        except TimeoutError:
            _LOG.warning("Performance background render timed out")
            embed = discord.Embed(
                title="⏳ Performance read delayed",
                description=(
                    "Saved prediction results are taking too long to load. "
                    "Refresh to retry; no result was estimated."
                ),
                color=discord.Color.orange(),
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("Performance background render failed")
            embed = discord.Embed(
                title="Performance temporarily unavailable",
                description="The evidence could not be read. No accuracy was inferred.",
                color=discord.Color.orange(),
            )
        async with self._redraw_lock:
            if self._render_revision["value"] != revision:
                return
            try:
                await asyncio.wait_for(
                    interaction.edit_original_response(
                        embed=embed, view=self._display_generation(),
                    ), timeout=4,
                )
            except (discord.HTTPException, TimeoutError):
                _LOG.warning("Performance background display update failed")

    async def _finish_forecast(
        self, interaction: discord.Interaction, revision: int,
    ) -> None:
        try:
            embed = await asyncio.wait_for(self.render(), timeout=9)
        except TimeoutError:
            embed = discord.Embed(
                title="⏳ Market research still unavailable",
                description=(
                    "The live model and venue-matched book did not finish "
                    "checking in time. No new verified signal. "
                    "Press Refresh Prediction to try again."
                ), color=discord.Color.orange(),
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("Background forecast render failed")
            embed = discord.Embed(
                title="⏳ Prediction unavailable",
                description="Couldn't verify live evidence. No trade inferred.",
                color=discord.Color.orange(),
            )
        async with self._redraw_lock:
            if self._render_revision["value"] != revision:
                return
            try:
                await asyncio.wait_for(
                    interaction.edit_original_response(
                        embed=embed, view=self._display_generation(),
                    ), timeout=4,
                )
            except (discord.HTTPException, TimeoutError):
                _LOG.warning("Background forecast display update failed")

    async def _redraw_locked(
        self, interaction: discord.Interaction, *,
        _background_retry: bool = False,
    ) -> None:
        started = time.monotonic()
        # Do not clear live components before the next embed is ready.
        render_timed_out = False
        try:
            embed = await asyncio.wait_for(self.render(), timeout=12)
        except TimeoutError:
            _LOG.warning("Guided render timed out page=%s elapsed_ms=%.0f",
                         self.page, (time.monotonic() - started) * 1000)
            render_timed_out = True
            embed = discord.Embed(
                title="⏳ Still Loading",
                description="This screen is taking longer than expected. Try again shortly.",
                color=discord.Color.orange(),
            )
        except (QuoteUnavailable, ValueError, ParlayBuildError) as exc:
            embed = discord.Embed(
                title="⏳ Not Available Right Now",
                description=f"{exc}\nNo invented prices or bets.",
                color=discord.Color.orange(),
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("Guided home render failed")
            embed = discord.Embed(
                title="⏳ This Screen Is Unavailable",
                description="We couldn't verify these details. Try Home or Back.",
                color=discord.Color.orange(),
            )
        _LOG.info("Guided render page=%s elapsed_ms=%.0f",
                  self.page, (time.monotonic() - started) * 1000)
        edit_failed = False
        try:
            await asyncio.wait_for(
                interaction.edit_original_response(
                    content=None, embed=embed, view=self._display_generation(),
                ),
                timeout=8,
            )
        except (TimeoutError, discord.HTTPException) as exc:
            edit_failed = True
            _LOG.warning("Guided menu edit failed page=%s error=%s", self.page, type(exc).__name__)
            # A failed edit leaves the previous generation intact.
            if _background_retry:
                # A background retry must never spam the user: the
                # loading embed is still there, and the next tap retries.
                return
            # The interaction was already acknowledged. Keep the user informed
            # without turning an edit failure into a silent dead menu.
            try:
                await interaction.followup.send(
                    "The menu could not update. Open /rival for a fresh menu.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                _LOG.warning("Guided menu fallback message unavailable")
        if (render_timed_out or edit_failed) and not _background_retry:
            # One self-healing attempt: a timed-out render left a "Still
            # Loading" embed with no recovery, and a failed edit left the
            # store pointing at detached items. Re-render once after a
            # short pause; the redraw lock serializes it against the
            # user's next tap, and the retry itself never retries again.
            _spawn(
                self._retry_failed_render(interaction),
                name="rival-guided-render-retry",
            )

    async def _retry_failed_render(
        self, interaction: discord.Interaction,
    ) -> None:
        try:
            await asyncio.sleep(2)
            async with self._redraw_lock:
                await self._redraw_locked(
                    interaction, _background_retry=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a retry must never break the loop
            _LOG.exception("Guided background render retry failed")

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        if action == "hunt-tools":
            # Old navigation controls can open the one notification center
            # without touching PostgreSQL or the shared redraw lock.
            fresh = self._display_generation()
            fresh.navigate("hunt")
            fresh._buttons()
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🔔 Notifications",
                    description="Track Crypto, watch an exchange forecast, or Stop All.",
                    color=discord.Color.blurple(),
                ), view=fresh, ephemeral=True,
            )
            return
        if action == "explain":
            await interaction.response.send_message(
                EXPLAIN[self.page], ephemeral=True,
            )
            return
        if action in ("zip", "state", "target", "funds"):
            await interaction.response.send_modal(_Entry(self, action))
            return
        if action == "cdna-auto":
            if self.page != "crypto-desk" or self.live_source != "draftkings":
                await interaction.response.send_message(
                    "Choose DraftKings (not connected) for independent references.", ephemeral=True,
                )
                return
            if self.cdna_personal is None:
                await interaction.response.send_message(
                    "CDNA personal research isn't configured.", ephemeral=True,
                )
                return
            # A remote Discord application ownership check can consume the
            # full interaction ACK window; defer BEFORE performing it.
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not await verify_cdna_owner(interaction, self.owner_id):
                await interaction.followup.send(
                    "Only the verified application owner may use personal CDNA data.",
                    ephemeral=True,
                )
                return
            try:
                results = await asyncio.wait_for(
                    self.cdna_personal.discover_coin(self.coin), timeout=11,
                )
                await interaction.edit_original_response(
                    embed=cdna_coin_embed(self.coin, results),
                )
            except (TimeoutError, Exception):  # noqa: BLE001
                _LOG.exception("CDNA owner-requested coin discovery unavailable")
                await interaction.edit_original_response(
                    content=(
                        "CDNA lookup unavailable. NO BET. "
                        "Independent exchange AI is still separate."
                    ),
                )
            return
        if action in ("cdna-search", "cdna-quote"):
            if self.page != "crypto-desk" or self.live_source != "draftkings":
                await interaction.response.send_message(
                    "Choose DraftKings Assisted research first.", ephemeral=True,
                )
            elif self.cdna_personal is None:
                await interaction.response.send_message(
                    "Personal CDNA lookup is not configured.", ephemeral=True,
                )
            else:
                await interaction.response.send_modal(
                    CDNAPersonalModal(
                        owner_id=self.owner_id, reader=self.cdna_personal,
                        search=action == "cdna-search",
                    ),
                )
            return
        if action == "hunt-config":
            # Acknowledge old settings-button interactions before any read.
            await interaction.response.defer(ephemeral=True, thinking=True)
            self.navigate("hunt")
            await self.redraw(interaction)
            return
        if action == "api-limits":
            # Owner lookup is a remote Discord request. Acknowledge
            # immediately, then use the followup to open the settings.
            await interaction.response.defer(ephemeral=True, thinking=True)
            application = await asyncio.wait_for(
                interaction.client.application_info(), timeout=3,
            )
            owner = application.owner
            if owner is None or owner.id != interaction.user.id:
                await interaction.followup.send(
                    "Only the application owner can edit API limits.", ephemeral=True,
                )
                return
            limits = OwnerLimitStore(interaction.client.store)
            view = OwnerLimitsView(
                store=limits, owner_id=owner.id,
                odds_provider=getattr(interaction.client, "oddspapi_provider", None),
                sports_provider=getattr(interaction.client, "current_provider", None),
            )
            await interaction.followup.send(
                "API Usage & Limits: review your saved limits, change a ceiling "
                "with confirmation, or check current provider usage. "
                "Provider allowances still take precedence.",
                view=view, ephemeral=True,
            )
            return
        if action == "manual":
            await interaction.response.defer(ephemeral=True, thinking=True)
            saved = await asyncio.wait_for(
                asyncio.to_thread(
                    self.location.get_saved, platform="discord",
                    user_id=str(self.owner_id),
                ), timeout=3,
            )
            if saved is None:
                await interaction.followup.send(
                    "Choose your betting state first.", ephemeral=True,
                )
                return
            books = set(saved.eligible_books)
            picker = await asyncio.to_thread(
                ManualParlayView, owner_id=self.owner_id,
                store=self.builder.store, eligible_books=books,
            )
            await interaction.followup.send(
                embed=picker.render(), view=picker, ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=False)
        _LOG.info("Guided interaction acknowledged action=%s", action)
        if action == "dk-independent-ai":
            if self.live_source != "draftkings" or self.research_factory is None:
                self.note = "Choose DraftKings and enable independent research first."
            else:
                try:
                    panel = self.research_factory(self.owner_id)
                    panel.asset = self.coin
                    panel.screen = "ai"
                    panel.section = "overview"
                    if panel.ai is not None and panel.ai.available and panel.market is not None:
                        try:
                            # Explicit click only; evidence gates apply unchanged.
                            # The AI server allows 40 s for an answer and the
                            # client 52 s, so waiting only 22 s here threw away
                            # answers that were still coming. Outlast both.
                            panel.opinion = await asyncio.wait_for(
                                panel.ai.ask(
                                    asset=self.coin, venues=panel.venues,
                                    requester_id=interaction.user.id,
                                ),
                                timeout=55,
                            )
                        except Exception as exc:  # noqa: BLE001
                            # No provider data, API credentials or internal paths
                            # may leak in exception messages to Discord.
                            _LOG.info("Requested independent AI unavailable: %s", type(exc).__name__)
                            panel.message = (
                                "Independent AI has no qualified response right now. "
                                "NO BET. Math and market lookup remain separate."
                            )
                    else:
                        panel.message = (
                            "Independent exchange AI is not configured or no "
                            "validated exchange evidence is connected. NO BET."
                        )
                    panel._rebuild()
                    await interaction.followup.send(
                        content=(
                            f"Independent {self.coin} exchange AI research. "
                            "RivaL uses its model-supported horizons, which may "
                            f"differ from the {self.horizon}m selected market window. "
                            "AI NEVER receives Crypto.com/CDNA data or DraftKings "
                            "quotes. Its lean is NOT a matched DraftKings bet."
                        ),
                        embed=await asyncio.wait_for(panel.render(), timeout=10),
                        view=panel, ephemeral=True,
                    )
                    self.note = (
                        "Independent AI requested with exchange-only inputs. "
                        "No Crypto.com data went to a model or training."
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Independent DraftKings-adjacent AI failed")
                    self.note = "Independent AI research unavailable; no market call."
        elif action == "desk-call":
            if self.live_source is None:
                self.note = "Choose a market first. No call was requested."
            elif self.live_source == "exchange":
                self.navigate("forecast")
            elif self.live_source == "polymarket":
                self.navigate("live")
                board = getattr(
                    getattr(self.auto_hunt, "bot", None),
                    "polymarket_board", None,
                )
                if callable(getattr(board, "request_contract_ai", None)):
                    try:
                        started = await asyncio.wait_for(
                            board.request_contract_ai(self.coin, self.horizon),
                            timeout=2.5,
                        )
                        if started:
                            self.note = (
                                "Requested one optional free AI contract review. "
                                "Mathematical learning remains independent."
                            )
                        else:
                            self.note = (
                                "No fresh contract available for an AI review. "
                                "No AI request was made."
                            )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("User-initiated contract AI review unavailable")
                        self.note = (
                            "Could not confirm a contract-specific AI request. "
                            "The market and math remain available."
                        )
            elif self.live_source == "kalshi":
                self.navigate("live")
            else:
                self.note = (
                    "DraftKings Predictions is not connected. "
                    "No market call was generated or substituted."
                )
        elif action == "desk-track":
            if self.live_source == "polymarket":
                tracker = getattr(
                    getattr(self.auto_hunt, "bot", None),
                    "polymarket_board", None,
                )
                if tracker is None:
                    self.note = "Polymarket tracking is unavailable."
                else:
                    try:
                        ok, message = await asyncio.wait_for(
                            tracker.start(
                                interaction.user, asset=self.coin,
                                horizon=self.horizon,
                            ), timeout=12,
                        )
                        self.note = ("✅ " if ok else "⏳ ") + message
                    except Exception:  # noqa: BLE001
                        _LOG.exception("Starting selected Polymarket tracker failed")
                        self.note = "Tracking could not be confirmed."
            elif self.live_source == "exchange":
                try:
                    ok, message = await self.targets.follow(
                        interaction.user, asset=self.coin,
                        horizon_minutes=self.horizon,
                    )
                    self.note = ("✅ " if ok else "⏳ ") + message
                except Exception:  # noqa: BLE001
                    _LOG.exception("Following selected exchange forecast failed")
                    self.note = "Forecast follow could not be confirmed."
            else:
                self.note = (
                    "Choose a supported tracking market first. "
                    "Kalshi is browse-only; DraftKings Predictions isn't connected."
                )
        elif action == "live" and self.live_source is None:
            # Legacy Live Mode buttons must not silently choose Polymarket.
            self.navigate("crypto-desk")
        elif action == "live":
            # Refresh on the live page: re-read the current (and next) contract.
            self.navigate("live")
            self.note = None
        elif action == "back":
            self.page = self.back_stack.pop() if self.back_stack else "home"
            self.note = None
        elif action in ("live-poly", "live-kalshi", "live-dk", "kalshi"):
            # Old unsupported DraftKings shortcut no longer presents fake calls.
            self.live_source = {
                "live-poly": "polymarket", "live-kalshi": "kalshi",
                "kalshi": "kalshi", "live-dk": None,
            }[action]
            self.navigate("live" if self.live_source is not None else "crypto-desk")
        elif action in ("home", "discover", "sports", "crypto", "crypto-desk", "hunt", "live", "practice",
                        "results", "performance", "help", "advanced", "feeds", "trust"):
            self.navigate(action)
            if action == "sports":
                await self._refresh_odds()
        elif action == "kalshi-refresh":
            self.live_source = "kalshi"
            self.navigate("live")
            self._kalshi_refresh = True
        elif action == "refresh":
            self.note = await self._refresh_odds()
        elif action in ("slip", "lower", "balanced", "aggressive"):
            self.risk = {
                "lower": RiskMode.LOWER, "balanced": RiskMode.BALANCED,
                "aggressive": RiskMode.AGGRESSIVE,
            }.get(action, self.risk)
            await self._refresh_odds()
            try:
                self.slip = await asyncio.to_thread(
                    self.builder.build_for_user,
                    platform="discord", user_id=str(self.owner_id),
                    leg_count=3, risk=self.risk,
                )
                self.navigate("slip")
            except ParlayBuildError as exc:
                self.slip = None
                self.note = f"PASS: {exc}. Nothing was placed."
        elif action in ("forecast", "quote"):
            self.navigate(action)
        elif action == "stop-all":
            tracker = getattr(getattr(self.auto_hunt, "bot", None),
                              "polymarket_board", None)
            result = await stop_all_notifications(
                self.owner_id, auto_hunt=self.auto_hunt,
                targets=self.targets, tracker=tracker,
            )
            self.navigate("hunt")
            self.note = result
        elif action in ("poly-start", "poly-stop"):
            self.live_source = "polymarket"
            self.navigate("live")
            service = (
                getattr(self.auto_hunt.bot, "polymarket_board", None)
                if self.auto_hunt is not None else None
            )
            if service is None:
                self.note = "Automatic Polymarket tracking is unavailable."
            elif action == "poly-start":
                try:
                    ok, message = await asyncio.wait_for(
                        service.start(
                            interaction.user, asset=self.coin,
                            horizon=self.horizon,
                        ), timeout=12,
                    )
                    self.note = ("✅ " if ok else "⏳ ") + message
                except Exception:  # noqa: BLE001
                    _LOG.exception("Starting live Polymarket DM failed safely")
                    self.note = "Live DM tracking could not be confirmed."
            else:
                try:
                    ended = await asyncio.wait_for(
                        service.stop(self.owner_id), timeout=6,
                    )
                    self.note = (
                        "Automatic live market DM stopped."
                        if ended else "No automatic live market DM was active."
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Stopping live Polymarket DM failed safely")
                    self.note = "Could not confirm stopping the live tracker."
        elif action in ("live-on", "live-off"):
            self.navigate("hunt")
            self.note = (
                "Legacy price-shift DMs were retired because they are not "
                "predictions. Tracking and background learning are unaffected."
            )
        elif action in ("hunt-on", "hunt-poly", "hunt-off", "hunt-status",
                         "hunt-research", "hunt-qualified"):
            self.navigate("hunt")
            self.note = "Auto-Hunt retired. Use Track Crypto or Changes instead."
        elif action in ("follow", "stop"):
            try:
                if action == "stop":
                    tracker = getattr(
                        getattr(self.auto_hunt, "bot", None),
                        "polymarket_board", None,
                    )
                    result = await stop_all_notifications(
                        self.owner_id, auto_hunt=self.auto_hunt,
                        targets=self.targets, tracker=tracker,
                    )
                    self.navigate("hunt")
                    self.note = result
                else:
                    ok, message = await self.targets.follow(
                        interaction.user, asset=self.coin,
                        horizon_minutes=self.horizon,
                    )
                    self.note = ("✅ " if ok else "⏳ ") + message
            except (ValueError, discord.HTTPException) as exc:
                self.note = f"Could not update your watch: {exc}"
            except Exception:  # noqa: BLE001
                _LOG.exception("Guided target failed")
                self.note = "Alert status unavailable. No new alert confirmed."
        elif action in ("buy", "sell", "close"):
            try:
                quote = await self.quotes.get(self.coin)
                if action == "close":
                    trade = await asyncio.to_thread(
                        self.paper.close, user_id=str(self.owner_id),
                        asset=self.coin, quote=quote,
                    )
                    self.note = (
                        f"Practice trade closed. Estimated after-cost result: "
                        f"{trade.assumed_net_usdt:+,.2f} fake USDT."
                    )
                else:
                    side = "long" if action == "buy" else "short"
                    self.note = await asyncio.to_thread(
                        self.paper.open, user_id=str(self.owner_id),
                        position_id=str(interaction.id), side=side,
                        notional_usdt=25.0, quote=quote,
                    )
            except (ValueError, QuoteUnavailable) as exc:
                self.note = f"No fake trade placed: {exc}"
            except Exception:  # noqa: BLE001
                _LOG.exception("Guided practice trade failed")
                self.note = "Couldn't verify this practice trade. No order confirmed."
        elif action == "proof":
            if self.stream_shadow is not None:
                try:
                    result = await asyncio.to_thread(
                        self.stream_shadow.report,
                        asset=self.coin, horizon_minutes=self.horizon,
                    )
                    severe = result["costs"]["severe"]["mean_net_bps"]
                    self.note = (
                        f"{result['status']} • "
                        f"{result['closed']}/{result['eligible_windows']} "
                        "exchange-matched forward windows with quoted exits"
                        f" • {result['days']} UTC days"
                        + (f" • Severe-cost mean {severe:+.1f} bps"
                           if severe is not None else
                           " • No complete after-cost evidence yet")
                        + ". Real-money profit NOT PROVEN."
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Guided strict quote proof unavailable")
                    self.note = "Source-matched forward profitability unavailable. NOT PROVEN."
            elif self.profit is None:
                self.note = "Live profit verification unavailable. NOT PROVEN."
            else:
                try:
                    result = await asyncio.to_thread(
                        self.profit.proof,
                        asset=self.coin, horizon_minutes=self.horizon,
                    )
                    self.note = (
                        f"{result.verified_quote_exits} of "
                        f"{result.eligible_forecast_windows} legacy forecast windows "
                        "have observed entry/exit quotes. "
                        "Unmatched markets may exist. Profit NOT PROVEN."
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Guided legacy profitability unavailable")
                    self.note = "Cannot verify profitability. NOT PROVEN."
        elif action in ("research", "track-forward"):
            if self.research_factory is None:
                self.note = "Advanced research is not configured."
            else:
                try:
                    panel = self.research_factory(self.owner_id)
                    # Bot Performance's drill-down must open its own evidence,
                    # not send users through an unrelated Opportunities tab.
                    if (self.page == "performance"
                        and hasattr(panel, "screen")
                        and hasattr(panel, "_rebuild")):
                        panel.asset = self.coin
                        panel.screen = "accuracy"
                        panel.section = ("tracker" if action == "track-forward"
                                         else "lineage")
                        panel._rebuild()
                    await interaction.followup.send(
                        embed=await panel.render(), view=panel,
                        ephemeral=True,
                    )
                    self.note = (
                        "Opened tracking or technical research in a private message. "
                        "This Home menu is still here."
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Advanced panel failed")
                    self.note = "Technical research unavailable."
        await self.redraw(interaction)

    async def submit(
        self, interaction: discord.Interaction, kind: str, value: str,
    ) -> None:
        try:
            if kind == "state":
                location = await asyncio.to_thread(
                    self.location.choose_state, platform="discord",
                    user_id=str(self.owner_id), state=value,
                )
                self.page = "sports"
                self.note = f"Saved {location.state_name}."
                await self._refresh_odds()
            elif kind == "zip":
                location = await self.location.choose_zip(
                    platform="discord", user_id=str(self.owner_id),
                    zip_code=value,
                )
                self.page = "sports"
                self.note = f"Saved {location.state_name}; ZIP was not saved."
                await self._refresh_odds()
            elif kind == "target":
                from parlay_bot.discord_app.price_targets import parse_price

                price = parse_price(value)
                ok, message = await self.targets.follow(
                    interaction.user, asset=self.coin,
                    horizon_minutes=self.horizon, target_price=price,
                )
                self.note = ("✅ " if ok else "⏳ ") + message
            elif kind == "funds":
                summary = await asyncio.to_thread(
                    self.paper.fund, str(self.owner_id),
                    starting_usdt=float(value),
                )
                self.note = (
                    f"Starting imaginary funds: "
                    f"{summary['practice']['funded']:,.2f} USDT. "
                    "Existing records cannot be erased."
                )
        except (ValueError, ZipLookupError) as exc:
            self.note = f"Could not save: {exc}"
        except Exception:  # noqa: BLE001
            _LOG.exception("Guided input failed kind=%s", kind)
            self.note = "Couldn't verify the change. Please retry."
        await self.redraw(interaction)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception,
        item: discord.ui.Item,
    ) -> None:
        _LOG.error(
            "Guided home interaction failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        # A failed component must still leave a working button, instead of
        # the generic "Something went wrong" dead-end seen on production.
        fresh = self._display_generation()
        fresh.page = "home"
        fresh.back_stack = []
        fresh.note = (
            "That action couldn't be verified. No change was confirmed. "
            "This is a fresh menu; your saved choices have not been erased."
        )
        fresh._buttons()
        content = await fresh.render()
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=content, view=fresh, ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=content, view=fresh, ephemeral=True,
                )
        except discord.HTTPException:
            _LOG.warning("RivaL guided recovery message could not be delivered")


# ---------------------------------------------------------------------------
# Stale-menu recovery: persistent fallback views for guided menus.
#
# A live GuidedHome view dies two ways: its 15-minute timeout lapses, or the
# bot restarts (every Discloud deploy) and the in-memory view is gone. Either
# way Discord still delivers the tap, discord.py finds no view for it, and the
# tap is silently discarded -- the user sees "This interaction failed" and
# the menu looks dead. These persistent views (timeout=None, registered with
# bot.add_view at startup) carry the SAME custom_ids as the live menu, so
# discord.py falls back to them when the live view is gone. They never
# execute a stateful action from stale state: navigation and read-only pages
# reopen on a fresh menu, and anything that would place, change, or stop
# something (trades, alerts, slips, location, limits) just reopens a fresh
# home menu with an honest note. A view has five rows. A fallback with two full-width selects must use at
# most 15 buttons; we split these custom_ids across three views;
# discord.py matches by custom_id across every registered persistent view.
# ---------------------------------------------------------------------------

# Pages a stale tap may safely reopen on a fresh menu. Everything else is a
# stateful action and is never run from stale state.
_STALE_SAFE_PAGES = {
    "home": "home",
    "discover": "crypto-desk",
    "sports": "sports",
    "crypto": "crypto",
    "hunt": "hunt",
    "hunt-tools": "hunt",
    "live": "crypto-desk",
    "crypto-desk": "crypto-desk",
    "practice": "practice",
    "results": "results",
    "performance": "performance",
    "help": "help",
    "advanced": "advanced",
    "feeds": "feeds",
    "trust": "trust",
    "back": "home",
    "forecast": "forecast",
    "quote": "quote",
    # Both open research from Bot Performance; a stale tap reopens that page
    # (read-only) instead of silently failing or dropping to Home.
    "track-forward": "performance",
    "research": "performance",
}

_STALE_NOTE = (
    "This menu expired, so it was refreshed. Your last tap did not place, "
    "change, or stop anything. Use this fresh menu."
)

# Button actions split across two views to stay under the 25-component limit.
_STALE_BUTTONS_A = (
    "sports", "crypto", "hunt", "practice", "results", "performance", "help", "advanced",
    "state", "zip", "refresh", "manual", "slip", "lower", "balanced",
    "aggressive", "forecast", "follow", "target", "home", "back",
    "hunt-poly", "hunt-research", "hunt-qualified", "track-forward",
)
_STALE_BUTTONS_B = (
    "quote", "stop", "buy", "sell", "close", "funds", "proof", "trust",
    "feeds", "research", "api-limits", "hunt-on",
    "hunt-off", "hunt-status", "explain",
)


_STALE_BUTTONS_C = (
    "live", "live-on", "live-off", "poly-start", "poly-stop",
    "hunt-tools", "kalshi", "kalshi-refresh",
    "live-poly", "live-kalshi", "live-dk", "stop-all",
    "crypto-desk", "desk-call", "desk-track",
    "discover", "hunt-config", "cdna-auto", "cdna-quote",
    "dk-independent-ai",
)


class _StaleButton(discord.ui.Button["_StaleGuidedMenuBase"]):
    """Dispatch-only button: never rendered, only matched by custom_id."""

    def __init__(self, action: str) -> None:
        super().__init__(
            label=action, custom_id=f"rival:guided:{action}",
            style=discord.ButtonStyle.secondary,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.recover(interaction, action=self.action)


class _StaleMarket(discord.ui.Select["_StaleGuidedMenuBase"]):
    """Recover an explicit market selection after a bot restart."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose a market",
            custom_id="rival:guided:market",
            options=[
                discord.SelectOption(label=MARKET_NAMES[value], value=value)
                for value in MARKET_NAMES
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.recover(interaction, market=self.values[0])


class _StaleCoin(discord.ui.Select["_StaleGuidedMenuBase"]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose a coin", custom_id="rival:guided:coin",
            options=[
                discord.SelectOption(label=f"{name} ({code})", value=code)
                for code, name in (
                    ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana"),
                    ("XRP", "XRP"),
                )
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.recover(interaction, coin=self.values[0])


class _StaleHorizon(discord.ui.Select["_StaleGuidedMenuBase"]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose how far ahead", custom_id="rival:guided:horizon",
            options=[
                discord.SelectOption(label=f"{n} minutes", value=str(n))
                for n in RESEARCH_HORIZONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.recover(
            interaction, horizon=int(self.values[0]),
        )


class _StaleGuidedMenuBase(discord.ui.View):
    """Persistent fallback for one slice of the guided menu's custom_ids."""

    def __init__(self, home_factory: Callable[[int], GuidedHome]) -> None:
        super().__init__(timeout=None)
        self.home_factory = home_factory

    async def recover(
        self, interaction: discord.Interaction, *, action: str | None = None,
        coin: str | None = None, horizon: int | None = None,
        market: str | None = None,
    ) -> None:
        await interaction.response.defer()
        home = self.home_factory(interaction.user.id)
        if coin is not None and coin in COINS:
            home.coin = coin
        if horizon is not None and horizon in RESEARCH_HORIZONS:
            home.horizon = horizon
        if market in MARKET_NAMES:
            home.live_source = market
            home.navigate("crypto-desk")
        elif action in _STALE_SAFE_PAGES:
            home.navigate(_STALE_SAFE_PAGES[action])
        elif coin is not None or horizon is not None:
            home.navigate("crypto-desk")
        home.note = _STALE_NOTE
        _LOG.info(
            "Stale guided menu recovered action=%s user_id=%s",
            action or f"select coin={coin} horizon={horizon}",
            interaction.user.id,
        )
        await home.redraw(interaction)


class StaleGuidedMenuViewA(_StaleGuidedMenuBase):
    """First slice of the stale-menu fallback (24 buttons; no selects)."""

    def __init__(self, home_factory: Callable[[int], GuidedHome]) -> None:
        super().__init__(home_factory)
        for action in _STALE_BUTTONS_A:
            self.add_item(_StaleButton(action))


class StaleGuidedMenuViewB(_StaleGuidedMenuBase):
    """Second slice (15 buttons + two full-row selects)."""

    def __init__(self, home_factory: Callable[[int], GuidedHome]) -> None:
        super().__init__(home_factory)
        for action in _STALE_BUTTONS_B:
            self.add_item(_StaleButton(action))
        self.add_item(_StaleCoin())
        self.add_item(_StaleHorizon())


class StaleGuidedMenuViewC(_StaleGuidedMenuBase):
    """Third slice for Live Mode, within Discord's five-row component limit."""

    def __init__(self, home_factory: Callable[[int], GuidedHome]) -> None:
        super().__init__(home_factory)
        for action in _STALE_BUTTONS_C:
            self.add_item(_StaleButton(action))
        self.add_item(_StaleMarket())


def build_stale_guided_menu_views(
    home_factory: Callable[[int], GuidedHome],
) -> tuple[StaleGuidedMenuViewA, StaleGuidedMenuViewB, StaleGuidedMenuViewC]:
    """Persistent fallbacks for every guided-menu custom_id.

    The select-bearing B view is capped at 15 buttons plus two selects.
    Extra Live Mode controls reside in a third independent persistent view,
    preserving restart recovery without exceeding Discord's five rows.
    """
    return (
        StaleGuidedMenuViewA(home_factory),
        StaleGuidedMenuViewB(home_factory),
        StaleGuidedMenuViewC(home_factory),
    )

def install_view_dispatch_logging(client: object) -> None:
    """Log-only wrapper around discord.py's ViewStore.dispatch_view.

    A tap that discord.py discards ("View interaction referencing unknown
    view") never reaches bot code, so its trigger could never be proven:
    the warning carries no message_id. This wrapper re-reads the same
    lookup before delegating and logs the message_id, custom_id, and
    whether the found item was detached (view is None) or unknown. It
    never changes dispatch behavior: any failure inside the logging
    leaves the original dispatch untouched, and double-install is a no-op.
    """
    try:
        store = client._connection._view_store  # type: ignore[attr-defined]
    except AttributeError:
        _LOG.warning("RivaL view dispatch logging unavailable: no view store")
        return
    if getattr(store, "_rival_dispatch_logging", False):
        return
    original = store.dispatch_view
    if not callable(original) or not hasattr(store, "_views"):
        _LOG.warning("RivaL view dispatch logging unavailable: unexpected store")
        return

    def _logged_dispatch(component_type: int, custom_id: str, interaction) -> None:
        try:
            message = getattr(interaction, "message", None)
            message_id = message.id if message is not None else None
            key = (component_type, custom_id)
            views = store._views
            item = None
            found_under = None
            if message_id is not None:
                item = views.get(message_id, {}).get(key)
                if item is not None:
                    found_under = "message"
            if item is None:
                item = views.get(None, {}).get(key)
                if item is not None:
                    found_under = "persistent"
            if item is None:
                _LOG.info(
                    "RivaL view dispatch unknown message_id=%s custom_id=%s",
                    message_id, custom_id,
                )
            elif item.view is None:
                # discord.py looks up the message-specific item BEFORE the
                # registered persistent view. A rebuild/timeout can leave a
                # detached item in that slot and shadow the healthy fallback.
                # Drop ONLY that unusable message-specific recoverable entry; then
                # let the original dispatcher find the normal safe fallback.
                fallback = views.get(None, {}).get(key)
                if (
                    found_under == "message"
                    and custom_id.startswith(("rival:guided:", "rival:research:", "rival:data:"))
                    and fallback is not None
                    and fallback.view is not None
                    and views.get(message_id, {}).get(key) is item
                ):
                    del views[message_id][key]
                    _LOG.warning(
                        "RivaL repaired detached recoverable component "
                        "message_id=%s custom_id=%s; persistent fallback ready",
                        message_id, custom_id,
                    )
                else:
                    _LOG.warning(
                        "RivaL view dispatch DISCARDED detached item "
                        "message_id=%s custom_id=%s found_under=%s item=%r",
                        message_id, custom_id, found_under, item,
                    )
        except Exception:  # noqa: BLE001 - logging must never break dispatch
            _LOG.exception("RivaL view dispatch logging failed")
        return original(component_type, custom_id, interaction)

    try:
        store.dispatch_view = _logged_dispatch
        store._rival_dispatch_logging = True
    except (AttributeError, TypeError):
        _LOG.warning("RivaL view dispatch logging could not be installed")
