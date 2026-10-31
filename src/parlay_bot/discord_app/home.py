"""Private, guided RivaL home for ordinary users, without losing advanced features."""
from __future__ import annotations

import asyncio
import logging
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
from parlay_bot.discord_app.polymarket_live_board import matching_call
from parlay_bot.discord_app.polymarket_watch import unavailable_text
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
COINS = RESEARCH_ASSETS
MARKET_NAMES = {
    "polymarket": "Polymarket Up/Down",
    "exchange": "Crypto perpetual futures",
    "kalshi": "Kalshi event contracts",
    "draftkings": "DraftKings Predictions • NOT CONNECTED",
}
EXPLAIN = {
    "home": "Crypto and Sports are for market research. Bot Performance shows forecast evidence; Practice contains only imaginary trading.",
    "discover": "Old menus can open Crypto or Practice. No actual trades are placed.",
    "crypto-desk": "Choose the platform you actually use. Event contracts (Polymarket, Kalshi, DraftKings Predictions) are NOT cryptocurrency perpetuals (Binance, Hyperliquid, OKX, etc.). Each platform needs separate verified calls and accuracy.",
    "live": "Track one verified Polymarket contract DM, updated only on meaningful changes. Contract AI calls are labeled separately and market prices are not win probabilities.",
    "hunt": "Track Polymarket in one live DM or follow an exchange forecast. Old price-shift DMs have been retired; Stop All cancels personal updates.",
    "hunt-tools": "Old Auto-Hunt notifications retired. Use Notifications.",
    "kalshi": "Kalshi is a different US prediction exchange. Its public prices and settlement rules must not inherit Polymarket Global forecast signals.",
    "sports": "Sports odds show possible payouts, not guaranteed wins. PASS means no reliable recommendation is available. Set your state to filter sportsbooks.",
    "slip": "A parlay combines picks. All legs must win for the parlay to win. No real wager is placed here.",
    "crypto": "Choose a coin and timeframe. UP/DOWN is a forecast, not permission to trade. A price target is a number to watch, not a predicted certainty.",
    "forecast": "A directional watch is research, not a proven profitable bet. Weak model probabilities are NO SIGNAL. Prices must match the forecast venue; genuine forward accuracy and expiry are shown.",
    "quote": "Ask is the price you might buy at; bid is the price you might sell at. These quotes are not guaranteed fills.",
    "practice": "Everything in Practice Money is fictional. Buying means you expect a rise; selling means you expect a fall. Fake gains never count as proven real profit.",
    "results": "Your personal practice results use only fictional trades and funds. They never count toward the bot's prediction record.",
    "performance": "Bot Performance reports source-specific prospective forecasts, settled results and learning evidence. Exchange forecasts do not score Polymarket contracts; counts of learned labels are not prediction wins.",
    "help": "Choose Crypto for separate event-contract platforms or leveraged crypto exchange forecasts. DraftKings Sportsbook futures are sports bets, not perpetual crypto futures. Sports is for sportsbook odds.",
    "advanced": "Optional checks for whether data is fresh and whether RivaL is improving. You do not need any of this to request a prediction.",
    "feeds": "Fresh means RivaL recently received a genuine completed market candle. That is different from a live streaming price or a prediction.",
    "trust": "A successful prediction is not necessarily a profitable trade. The combined model must beat the old model on later matched results and after trading costs.",
}


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


def _latest_forward_cycle(market: object) -> dict | None:
    """Latest forward-cycle row, or None when it cannot be read.

    Runs in a worker thread: the store connection is synchronous. Positional
    row access keeps this valid for both the SQLite and Postgres stores.
    """
    try:
        with market.connect() as conn:
            row = conn.execute(
                "SELECT checked_at, issued_forecasts, target_expired "
                "FROM perp_forward_cycle_diagnostics "
                "ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
    except Exception:  # noqa: BLE001 - health display must never break the menu
        return None
    if row is None:
        return None
    checked = row[0]
    if isinstance(checked, str):
        try:
            checked = datetime.fromisoformat(checked)
        except ValueError:
            return None
    if not isinstance(checked, datetime) or checked.tzinfo is None:
        if isinstance(checked, datetime):
            checked = checked.replace(tzinfo=UTC)
        else:
            return None
    try:
        issued = int(row[1] or 0)
        expired = int(row[2] or 0)
    except (TypeError, ValueError):
        return None
    return {"checked_at": checked, "issued": issued, "expired": expired}


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
    current = (
        snapshot is not None and snapshot.status == "ok"
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
    strength = (
        f"{snapshot.probability:.1%} raw score\n"
        f"{model_strength(snapshot.probability)}"
        + (
            f" • {snapshot.training_evaluated} walk-forward examples"
            if snapshot.training_evaluated else ""
        )
        if current and snapshot.probability is not None else "Unverified"
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
                self._add("Track in DMs", "poly-start", 2, "🔔", True)
                self._add("Stop Tracking", "poly-stop", 2, "🔕")
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
            self.add_item(_Market(self.live_source))
            self.add_item(_Coin(self.coin, row=1))
            if self.live_source != "kalshi":
                self.add_item(_PolymarketHorizon(
                    self.horizon, row=2, market=self.live_source or "polymarket",
                ))
            self._add("Learning Details", "trust", 3, "🧠", True)
            self._add("Price Feed Health", "feeds", 3, "📡")
            self._add("Advanced Tools", "advanced", 3, "⚙️")
        elif p == "help":
            self._add("Start with Sports", "sports", 0, "🏆")
            self._add("Analyze Crypto", "crypto-desk", 0, "📈")
            self._add("Practice Safely", "practice", 1, "💵")
        elif p == "advanced":
            self._add("Are My Prices Fresh?", "feeds", 0, "📡")
            self._add("Is RivaL Improving?", "trust", 0, "📊")
            self._add("Technical Research", "research", 1, "🧪")
            self._add("API Usage & Limits", "api-limits", 2, "🔒")
            self._add("Manual Price Target", "target", 2, "🎯")
        elif p in ("feeds", "trust"):
            self.add_item(_Coin(self.coin))
            if p == "trust":
                self.add_item(_Horizon(self.horizon))
            self._add("Refresh", p, 2, "🔄", True)
        if p != "home":
            self._add("Home", "home", 4, "🏠")
            self._add("Back", "back", 4, "↩️")

    def navigate(self, page: str) -> None:
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
                    "Find a live crypto market, compare sports odds, or "
                    "manage your personal tracking.\n"
                    "Private research only. No real bets or orders are placed."
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
                    name="What this market means",
                    value=(
                        "**Crypto perpetual futures:** Long/short exposure "
                        "on exchanges such as Hyperliquid, Binance Futures, "
                        "OKX, Gate, and Bitget. These are NOT DraftKings "
                        "Sportsbook futures or DraftKings Predictions.\n"
                        "RivaL reads exchange research, not a matched "
                        "YES/NO betting contract."
                    ), inline=False,
                )
                if self.market is None:
                    accuracy = "Exchange collector is not connected in this session."
                else:
                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                ForwardAccuracyMonitor(self.market).summary,
                                asset=self.coin,
                            ), timeout=1.3,
                        )
                        sample = result["last_7d"][self.horizon]
                        hits = sample["hit_rate"]
                        accuracy = (
                            f"{sample['settled']} verified settled of "
                            f"{sample['issued']} forecasts in the last 7 days.\n"
                            + (
                                f"Correct direction: {hits:.1%} "
                                "(forward exchange-candle outcomes)"
                                if hits is not None else
                                "Not enough verified forward outcomes for a reliable win rate."
                            )
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception(
                            "RivaL selected exchange results read failed "
                            "asset=%s horizon=%s", self.coin, self.horizon,
                        )
                        accuracy = (
                            "Could not load exchange results right now. "
                            "That does not establish the model's accuracy."
                        )
                embed.add_field(
                    name=f"Exchange forecast results • {self.coin} {self.horizon}m",
                    value=accuracy, inline=False,
                )
                embed.add_field(
                    name="Next action",
                    value="**Get Exchange Forecast** checks the exchange model; "
                          "**Follow Forecast** keeps one private watch. "
                          "This is not a Polymarket contract call.",
                    inline=False,
                )
            elif market == "polymarket":
                # Keep exact-contract evidence independent of historical
                # Drive imports, exchange promotion and checkpoint reads.
                poly_reader = self.polymarket_status or self.training_status
                if poly_reader is None:
                    poly = (
                        "Polymarket learning statistics aren't connected to "
                        "this menu. This is a data-read status, NOT a "
                        "confirmed Polymarket outage or a 0% win rate."
                    )
                else:
                    try:
                        status = await asyncio.wait_for(
                            asyncio.to_thread(
                                poly_reader, self.coin, self.horizon,
                            ), timeout=1.5,
                        )
                        learning = (
                            status if self.polymarket_status is not None
                            else status.get("polymarket_learning")
                        )
                        if not isinstance(learning, dict):
                            poly = (
                                "No Polymarket outcome summary was returned. "
                                "The exchange-candle archive cannot replace it."
                            )
                        else:
                            learned = int(learning.get("learned_contracts") or 0)
                            settled = int(learning.get("settled_forward_contracts") or 0)
                            model_brier = learning.get("forward_brier")
                            baseline_brier = learning.get("market_baseline_brier")
                            proof = (
                                f"Model Brier: {model_brier:.4f} • "
                                f"Market Brier: {baseline_brier:.4f}"
                                if model_brier is not None
                                and baseline_brier is not None
                                else "Not enough settled model forecasts "
                                     "to compare with market prices."
                            )
                            poly = (
                                f"Verified contract outcomes learned: **{learned}**\n"
                                f"Prospective model calls resolved: **{settled}**\n"
                                f"{proof}\n"
                                "These are Polymarket contracts only, "
                                "not exchange-futures predictions."
                            )
                    except Exception:  # noqa: BLE001
                        _LOG.exception(
                            "RivaL Polymarket learning query failed "
                            "asset=%s horizon=%s", self.coin, self.horizon,
                        )
                        poly = (
                            "Couldn't read Polymarket learning statistics "
                            "right now. Contract feed availability is a "
                            "different check. No win rate is being guessed."
                        )
                embed.add_field(
                    name=f"🧠 Polymarket model progress • {self.coin} {self.horizon}m",
                    value=poly[:1024], inline=False,
                )
                embed.add_field(
                    name="Is there a proven betting advantage?",
                    value=(
                        "**Not yet.** RivaL must beat this contract market's "
                        "own prices on enough future settled outcomes AND "
                        "after fees/spread. Training labels alone do not "
                        "prove an edge."
                    ), inline=False,
                )
                embed.add_field(
                    name="Next action",
                    value="**View Contract Call** checks the exact listed "
                          "UP/DOWN contract. **Track Market** keeps one "
                          "updated DM. A quote is not a verified win probability.",
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
                embed = discord.Embed(
                    title=f"🎯 Polymarket | {self.coin} {self.horizon}m",
                    description="**RivaL call: WAIT** • No verified contract-specific call yet.",
                    color=discord.Color.orange(),
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
                        "ON • This market updates in one DM"
                        if tracker.asset == self.coin and tracker.horizon == self.horizon
                        else f"ON • {tracker.asset} {tracker.horizon}m instead"
                    )
                else:
                    tracking = "OFF"
                embed.add_field(name="🔔 Your tracking", value=tracking, inline=False)
                cached = None
                if service is not None and callable(getattr(service, "preview", None)):
                    try:
                        cached = await service.preview(self.coin, self.horizon)
                    except Exception:  # noqa: BLE001
                        _LOG.info("Exact Polymarket preview unavailable")
                board, call = cached if cached is not None else (None, None)
                if board is None or board.current is None:
                    embed.add_field(
                        name="Contract",
                        value="No verified current contract. No market price or call was guessed.",
                        inline=False,
                    )
                else:
                    event, quote = board.current, board.current_quote
                    details = (
                        f"[Open contract]({event.url}) • "
                        f"Closes {discord.utils.format_dt(event.closes, style='R')}"
                    )
                    if event.price_to_beat is not None:
                        details += f"\nPrice to beat: USD {event.price_to_beat:,.4f}"
                    if quote is not None:
                        details += (
                            f"\nUP ask USD {quote.up_ask:.2f} • "
                            f"DOWN ask USD {quote.down_ask:.2f} "
                            f"({discord.utils.format_dt(quote.quote_at, style='R')})"
                        )
                    else:
                        details += "\n" + unavailable_text(board.quote_unavailable_reason)
                    embed.add_field(name="📈 Verified market", value=details, inline=False)
                    if matching_call(board, call, asset=self.coin, horizon=self.horizon):
                        embed.description = (
                            f"**RivaL {call.kind} call: {call.direction}** "
                            "• experimental, NOT a verified profitable edge."
                        )
                        embed.add_field(
                            name="🧠 Why RivaL called it",
                            value=(
                                f"{call.thesis[:450]}\n"
                                f"Issued {discord.utils.format_dt(call.issued_at, style='R')} "
                                "for this exact contract."
                            ), inline=False,
                        )
                    else:
                        research = getattr(service, "research", None)
                        reason = (
                            research.status(event.slug) if research is not None
                            else "Free contract AI or verified inputs unavailable."
                        )
                        embed.add_field(
                            name="🧠 Why no call?",
                            value=str(reason)[:320],
                            inline=False,
                        )
                embed.set_footer(text="Share prices are not winning odds • No orders")
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
                value=(
                    "Sports & Odds shows sportsbook prices and screens, "
                    "not model predictions. The research model covers "
                    "soccer and basketball only, once a league has 130 "
                    "finalized games. NFL has odds and slips but no model "
                    "forecast."
                ),
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
                title=("💵 My Practice Money" if p == "practice" else "📊 My Results"),
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
        elif p == "feeds":
            embed = discord.Embed(
                title=f"📡 Are the Prices Fresh? | {self.coin}",
                description=(
                    "RivaL checks multiple exchanges automatically. You only "
                    "need to know whether their recent price data is usable. "
                    "These are **5-minute market updates**, not 10 live "
                    "streaming connections."
                ),
                color=discord.Color.orange(),
            )
            if self.market is None or not self.venues:
                embed.add_field(
                    name="Exchange data",
                    value="Market collection is not connected. No freshness claimed.",
                    inline=False,
                )
            else:
                try:
                    rows = await asyncio.to_thread(self.market.feed_health)
                    found = {(r["venue"], r["asset"]): r for r in rows}
                    now = datetime.now(UTC)
                    healthy = []
                    stale = []
                    for venue in self.venues:
                        row = found.get((venue, self.coin))
                        recent = False
                        if row and row.get("last_success_at") and row.get("last_candle_close"):
                            try:
                                success_at = datetime.fromisoformat(
                                    str(row["last_success_at"])
                                ).astimezone(UTC)
                                candle_at = datetime.fromisoformat(
                                    str(row["last_candle_close"])
                                ).astimezone(UTC)
                                recent = (
                                    row["status"] == "ok"
                                    and timedelta(0) <= now - success_at <= timedelta(minutes=15)
                                    and timedelta(0) <= now - candle_at <= timedelta(minutes=15)
                                )
                            except (ValueError, TypeError):
                                pass
                        (healthy if recent else stale).append(venue)
                    embed.add_field(
                        name=f"{len(healthy)} of {len(self.venues)} exchanges have fresh data",
                        value=(
                            f"✅ Working: {', '.join(healthy) or 'None verified'}\n"
                            f"⏳ Not verified: {', '.join(stale) or 'None'}"
                        ),
                        inline=False,
                    )
                    if healthy:
                        embed.color = discord.Color.blurple()
                    try:
                        health_lines: list[str] = []
                        successes: list[datetime] = []
                        for r in rows:
                            if r.get("status") != "ok" or not r.get("last_success_at"):
                                continue
                            try:
                                successes.append(datetime.fromisoformat(
                                    str(r["last_success_at"])
                                ).astimezone(UTC))
                            except (ValueError, TypeError):
                                continue
                        if successes:
                            age = max(0, int((now - max(successes)).total_seconds() // 60))
                            verdict = "fresh" if age <= 15 else "stale"
                            health_lines.append(
                                f"Crypto ingestion: {verdict} ({age} min ago)"
                            )
                        else:
                            health_lines.append(
                                "Crypto ingestion: no successful ingestion recorded"
                            )
                        cycle = await asyncio.to_thread(
                            _latest_forward_cycle, self.market,
                        )
                        if cycle is None:
                            health_lines.append("Forecast cycle: no cycle recorded yet")
                        else:
                            age = max(0, int(
                                (now - cycle["checked_at"]).total_seconds() // 60
                            ))
                            verdict = "fresh" if age <= 10 else "stale"
                            health_lines.append(
                                f"Forecast cycle: {verdict} — "
                                f"{cycle['issued']} issued, "
                                f"{cycle['expired']} expired ({age} min ago)"
                            )
                        if self.collector is not None:
                            last = await asyncio.to_thread(
                                self.collector._last_refresh,
                            )
                            if last is None:
                                health_lines.append(
                                    "Sports refresh: no completed refresh yet"
                                )
                            else:
                                age = max(0, int(
                                    (now - last).total_seconds() // 60
                                ))
                                interval = getattr(
                                    self.collector, "refresh_interval",
                                    timedelta(minutes=30),
                                )
                                verdict = (
                                    "fresh" if timedelta(minutes=age) <= interval
                                    else "update due"
                                )
                                health_lines.append(
                                    f"Sports refresh: {verdict} ({age} min ago)"
                                )
                        if health_lines:
                            embed.add_field(
                                name="System health",
                                value="\n".join(health_lines),
                                inline=False,
                            )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("RivaL system health unavailable")
                        embed.add_field(
                            name="System health",
                            value="Health details unavailable.",
                            inline=False,
                        )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL beginner exchange freshness unavailable")
                    embed.add_field(
                        name="Exchange data",
                        value="Cannot verify current exchange updates. No claims of freshness.",
                        inline=False,
                    )
        elif p == "trust":
            embed = discord.Embed(
                title=f"📊 Is RivaL Actually Improving? | {self.coin} {self.horizon}m",
                description=(
                    "RivaL compares the new combined model against the old "
                    "model using predictions made **before** prices changed. "
                    "Until it proves better after trading costs, both remain "
                    "research, not winning bets."
                ),
                color=discord.Color.orange(),
            )
            if self.multi is None:
                embed.add_field(
                    name="Current verdict",
                    value="Not verified. The combined-model comparison isn't connected.",
                    inline=False,
                )
            else:
                try:
                    proof = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.multi.report,
                            asset=self.coin, horizon_minutes=self.horizon,
                        ), timeout=2.5,
                    )
                    settled = proof["settled"]
                    is_better = proof.get("brier_improvement")
                    performance = (
                        "Insufficient future results"
                        if is_better is None else
                        ("Combined model has lower prediction error so far"
                         if is_better > 0 else
                         "Combined model has NOT beaten the original so far")
                    )
                    embed.add_field(
                        name="What the evidence says",
                        value=(
                            f"**{performance}.**\n"
                            f"Completed future comparisons: **{settled}**\n"
                            f"Days measured: **{proof['days']}**\n"
                            f"Exchanges contributing: **up to {proof['max_sources']}**"
                        ),
                        inline=False,
                    )
                    embed.add_field(
                        name="Would I trust it with real money?",
                        value=(
                            "**Not yet.** The models need at least 150 paired "
                            "future outcomes over 21 days and positive results "
                            "after realistic costs. No real orders are placed."
                        ),
                        inline=False,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL beginner combined-model proof unavailable")
                    embed.add_field(
                        name="Current verdict",
                        value="Accuracy evidence is unavailable. No improvement claimed.",
                        inline=False,
                    )
            if self.training_status is not None:
                try:
                    state = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.training_status, self.coin, self.horizon,
                        ), timeout=2.5,
                    )
                    latest = state.get("live_learning")
                    last_live = (
                        str(latest.get("completed_at"))
                        if isinstance(latest, dict) else "Not recorded",
                    )
                    if isinstance(last_live, tuple):
                        last_live = last_live[0]
                    embed.add_field(
                        name="Live training and active model",
                        value=(
                            f"Active engine: **{state['live_engine']}**\n"
                            f"Latest live learning cycle: **{last_live}**\n"
                            f"Historical challenger serving: **{'YES' if state.get('active_offline_model') else 'NO'}**\n"
                            f"{state.get('reason', 'No qualified historical challenger')}"
                        ),
                        inline=False,
                    )
                    checkpoint = state.get("model_checkpoints")
                    if checkpoint:
                        embed.add_field(name="Persistent learned weights", value=(
                            f"Saved checkpoints: **{checkpoint['persisted_checkpoints']}** • "
                            f"reused this process: **{checkpoint['reused_this_process']}**\n"
                            "New resolved observations update the saved model once."
                        ), inline=False)
                    contract_learning = state.get("polymarket_learning")
                    if contract_learning:
                        embed.add_field(name="Polymarket outcome learning", value=(
                            f"Verified outcomes learned: **{contract_learning['learned_contracts']}**\n"
                            f"Settled model forecasts: **{contract_learning['settled_forward_contracts']}**\n"
                            "Exchange outcomes and AI confidence are excluded from these labels."
                        ), inline=False)
                    candidate = state.get("candidate")
                    if candidate:
                        accuracy = candidate.get("holdout_accuracy")
                        accuracy_display = (
                            f"{accuracy:.1%}" if accuracy is not None
                            else "Unavailable"
                        )
                        embed.add_field(
                            name="Six-month offline research",
                            value=(
                                f"Data version: **{candidate['data_version'][:12]}**\n"
                                f"Source months: **{candidate['source_months']}**\n"
                                f"Historical test accuracy: **{accuracy_display}**\n"
                                f"Last verified import: "
                                f"**{candidate['first_imported_at']}**\n"
                                "This is retrospective only, NOT measured live accuracy."
                            ),
                            inline=False,
                        )
                    else:
                        embed.add_field(
                            name="Six-month offline research",
                            value=(
                                "No checksum-verified offline candidate loaded "
                                "for this coin/timeframe yet. "
                                "A completed archive upload is not a trained "
                                "live model."
                            ),
                            inline=False,
                        )
                    embed.add_field(
                        name="Historical data read status",
                        value=(
                            f"Drive: **{state['drive_status']}**\n"
                            "Last historical workflow completion: **not "
                            "verified from this bot**. Scheduled workflow "
                            "and live model promotion are separate."
                        ),
                        inline=False,
                    )
                except (TimeoutError, Exception):  # noqa: BLE001
                    _LOG.exception("Training provenance status unavailable")
                    embed.add_field(
                        name="Historical training status",
                        value="Unverified right now. No active historical "
                              "candidate claimed.",
                        inline=False,
                    )
        elif p == "help":
            embed = discord.Embed(
                title="❓ Getting Started",
                description=(
                    "1. **Crypto:** inspect live markets or separate exchange forecasts.\n"
                    "2. **Sports:** choose your state and browse odds.\n"
                    "3. **Notifications:** enable tracking and stop it anytime.\n"
                    "4. **Bot Performance:** verify forecast scores and learning.\n"
                    "5. **Practice:** trade imaginary funds and see your own results.\n\n"
                    "**WAIT** means evidence is insufficient. No bets are placed."
                ), color=discord.Color.blurple(),
            )
        else:
            embed = discord.Embed(
                title="⚙️ Advanced Tools",
                description=(
                    "**Optional. You do not need this to get predictions.**\n"
                    "**Are My Prices Fresh?** checks real exchange updates.\n"
                    "**Is RivaL Improving?** compares the new and old predictions.\n"
                    "**Technical Research** opens the detailed model dashboard.\n"
                    "The old advanced shortcuts are available under "
                    "`/rival-tools` if you genuinely need them.\n"
                    "**No real bets are placed.**"
                ), color=discord.Color.blurple(),
            )
        if self.note:
            embed.add_field(name="What Happened", value=self.note[:1000],
                            inline=False)
        embed.set_footer(text="Private research • No orders or guaranteed accuracy")
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
            if self.page == "forecast" and isinstance(
                interaction, discord.Interaction,
            ):
                loading = discord.Embed(
                    title=f"🔄 Checking {self.coin} {self.horizon}m",
                    description=(
                        "Checking newly issued model forecasts, matching live "
                        "quotes, and real forward evidence. The previous "
                        "forecast is not automatically a new trade."
                    ),
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
                asyncio.create_task(
                    self._finish_forecast(interaction, revision),
                    name="rival-forecast-details",
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
            asyncio.create_task(self._retry_failed_render(interaction))

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
                            # Explicit click only. Existing free-model budget and
                            # independent exchange evidence gates apply unchanged.
                            panel.opinion = await asyncio.wait_for(
                                panel.ai.ask(
                                    asset=self.coin, venues=panel.venues,
                                    requester_id=interaction.user.id,
                                ),
                                timeout=22,
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
        elif action == "research":
            if self.research_factory is None:
                self.note = "Advanced research is not configured."
            else:
                try:
                    panel = self.research_factory(self.owner_id)
                    await interaction.followup.send(
                        embed=await panel.render(), view=panel,
                        ephemeral=True,
                    )
                    self.note = (
                        "Opened technical research in a separate private message. "
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
    "help": "help",
    "advanced": "advanced",
    "feeds": "feeds",
    "trust": "trust",
    "back": "home",
    "forecast": "forecast",
    "quote": "quote",
}

_STALE_NOTE = (
    "This menu expired, so it was refreshed. Your last tap did not place, "
    "change, or stop anything. Use this fresh menu."
)

# Button actions split across two views to stay under the 25-component limit.
_STALE_BUTTONS_A = (
    "sports", "crypto", "hunt", "practice", "results", "help", "advanced",
    "state", "zip", "refresh", "manual", "slip", "lower", "balanced",
    "aggressive", "forecast", "follow", "target", "home", "back",
    "hunt-poly", "hunt-research", "hunt-qualified",
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
    """First slice of the stale-menu fallback (23 buttons; no selects)."""

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
