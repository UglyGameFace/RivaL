"""Private, guided RivaL home for ordinary users, without losing advanced features."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.api_limits import OwnerLimitsView
from parlay_bot.discord_app.manual_parlay import ManualParlayView
from parlay_bot.discord_app.presentation import (
    parlay_embed,
)
from parlay_bot.discord_app.signals import model_strength, signal_embed
from parlay_bot.discord_app.targeting import target_snapshot
from parlay_bot.jurisdiction.zip_lookup import ZipLookupError
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import QuoteUnavailable, quote_embed
from parlay_bot.providers.owner_limits import OwnerLimitStore

if TYPE_CHECKING:
    from parlay_bot.collectors.current import CurrentBoardCollector
    from parlay_bot.discord_app.auto_hunt import AutoHuntService
    from parlay_bot.discord_app.targeting import DirectionTargetService
    from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
    from parlay_bot.parlays.builder import ParlayBuilder
    from parlay_bot.perpetuals.live_quotes import PublicBestQuoteClient
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
    from parlay_bot.perpetuals.paper_ledger import PaperLedger
    from parlay_bot.perpetuals.profit_proof import ProspectiveQuoteProfitMonitor
    from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger

_LOG = logging.getLogger(__name__)
COINS = ("BTC", "ETH", "SOL")
EXPLAIN = {
    "home": "Choose an area. Auto-Hunt scans for new research watches after you opt in. No wagers are placed.",
    "hunt": "Auto-Hunt scans genuine future-issued crypto predictions, checks the SAME venue's fresh bid/ask and forward research evidence, and sends new private notifications. It never invents a strike or places a bet. Raw model percentages are not calibrated winning odds.",
    "sports": "Sports odds show possible payouts, not guaranteed wins. PASS means no reliable recommendation is available. Set your state to filter sportsbooks.",
    "slip": "A parlay combines picks. All legs must win for the parlay to win. No real wager is placed here.",
    "crypto": "Choose a coin and timeframe. UP/DOWN is a forecast, not permission to trade. A price target is a number to watch, not a predicted certainty.",
    "forecast": "A directional watch is research, not a proven profitable bet. Weak model probabilities are NO SIGNAL. Prices must match the forecast venue; genuine forward accuracy and expiry are shown.",
    "quote": "Ask is the price you might buy at; bid is the price you might sell at. These quotes are not guaranteed fills.",
    "practice": "Everything in Practice Money is fictional. Buying means you expect a rise; selling means you expect a fall. Fake gains never count as proven real profit.",
    "results": "Practice results are your own fake trades. Model accuracy is measured separately from later verified market outcomes. Real profit is not yet proven.",
    "help": "Start at Home. For sports, select your state. For crypto, choose a coin and press Get Signal. To learn without real money, open Practice Money.",
    "advanced": "Optional checks for whether data is fresh and whether RivaL is improving. You do not need any of this to request a prediction.",
    "feeds": "Fresh means RivaL recently received a genuine completed market candle. That is different from a live streaming price or a prediction.",
    "trust": "A successful prediction is not necessarily a profitable trade. The combined model must beat the old model on later matched results and after trading costs.",
}


class _Button(discord.ui.Button["GuidedHome"]):
    def __init__(self, title: str, action: str, row: int,
                 emoji: str | None = None, primary: bool = False) -> None:
        super().__init__(
            label=title, custom_id=f"rival:guided:{action}", emoji=emoji,
            row=row, style=(discord.ButtonStyle.primary if primary
                            else discord.ButtonStyle.secondary),
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


class _Coin(discord.ui.Select["GuidedHome"]):
    def __init__(self, selected: str) -> None:
        names = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}
        super().__init__(
            placeholder="Choose a coin", row=0, custom_id="rival:guided:coin",
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
    def __init__(self, selected: int) -> None:
        super().__init__(
            placeholder="Choose how far ahead", row=1,
            custom_id="rival:guided:horizon",
            options=[
                discord.SelectOption(
                    label=f"{n} minutes", value=str(n), default=n == selected,
                ) for n in (5, 15)
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
        self.page = "home"
        self.back_stack: list[str] = []
        self.coin = "BTC"
        self.horizon = 5
        self.risk = RiskMode.BALANCED
        self.slip = None
        self.note: str | None = None
        self._redraw_lock = asyncio.Lock()
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
            items = (
                ("Sports & Odds", "sports", 0, "🏆"),
                ("Predict Crypto", "crypto", 0, "📈"),
                ("Practice Money", "practice", 0, "💵"),
                ("Auto-Hunt", "hunt", 2, "🔔"),
                ("My Results", "results", 1, "📊"),
                ("Help Me Start", "help", 1, "❓"),
                ("Advanced", "advanced", 1, "⚙️"),
            )
            for title, action, row, emoji in items:
                self._add(title, action, row, emoji, action == "sports")
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
            self._add("Get Signal", "forecast", 2, "🎯", True)
            self._add("Follow in DM", "follow", 2, "🔔")
            self._add("Auto-Hunt", "hunt", 2, "🔔")
            self._add("See Buy/Sell Prices", "quote", 3, "💱")
            self._add("Stop My Alerts", "stop", 3, "🔕")
        elif p == "hunt":
            self._add("Start Auto-Hunt", "hunt-on", 0, "🔔", True)
            self._add("Stop Auto-Hunt", "hunt-off", 0, "🔕")
            self._add("Check Status", "hunt-status", 1, "🔎")
        elif p == "practice":
            self.add_item(_Coin(self.coin))
            self._add("Practice Buy $25", "buy", 1, "📈", True)
            self._add("Practice Sell $25", "sell", 1, "📉")
            self._add("Close Fake Trade", "close", 2, "✅")
            self._add("Set Fake Balance", "funds", 2, "💵")
            self._add("See My Results", "results", 3, "📊")
        elif p == "results":
            self.add_item(_Coin(self.coin))
            self.add_item(_Horizon(self.horizon))
            self._add("Check Profit Evidence", "proof", 2, "🔍")
            self._add("Can I Trust Predictions?", "trust", 2, "✅")
        elif p == "help":
            self._add("Start with Sports", "sports", 0, "🏆")
            self._add("Try Crypto", "crypto", 0, "📈")
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
        self._add("Home", "home", 4, "🏠")
        self._add("Back", "back", 4, "↩️")
        self._add("Explain This", "explain", 4, "❓")

    def navigate(self, page: str) -> None:
        if page == self.page:
            return
        if page == "home":
            self.back_stack.clear()
        else:
            self.back_stack.append(self.page)
            self.back_stack = self.back_stack[-12:]
        self.page = page
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
                    "**Welcome! What would you like to do?**\n"
                    "🏆 **Sports & Odds**: Find games and compare prices.\n"
                    "📈 **Predict Crypto**: See BTC, ETH or SOL forecasts.\n"
                    "🔔 **Auto-Hunt**: Get new private research alerts automatically.\n"
                    "💵 **Practice Money**: Try fake funds, no deposits.\n"
                    "📊 **My Results**: See wins and losses.\n"
                    "❓ **Help Me Start**: Simple instructions.\n\n"
                    "**No real bets or orders are placed.**"
                ), color=discord.Color.blurple(),
            )
        elif p == "hunt":
            status = (
                await self.auto_hunt.status(self.owner_id)
                if self.auto_hunt is not None else
                "Unavailable until market collection is configured"
            )
            embed = discord.Embed(
                title="🔔 Auto-Hunt | Find Research Opportunities",
                description=(
                    "**" + status + "**\n\n"
                    "After you choose Start, RivaL watches BTC, ETH and SOL "
                    "for new 5-minute and 15-minute research forecasts. "
                    "It checks the prediction's own exchange for a fresh "
                    "market quote and recent measured forward evidence.\n\n"
                    "Only qualifying NEW observations trigger a fresh DM. "
                    "You do not need to type a price or keep a menu open.\n"
                    "**No automated real-money bets, guaranteed winning odds, "
                    "or verified profitable entry are implied.**"
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
            snapshot = await asyncio.to_thread(
                target_snapshot, self.market,
                asset=self.coin, horizon_minutes=self.horizon,
            )
            headline = (
                f"{snapshot.direction} lean • raw "
                f"{snapshot.probability:.1%} ({model_strength(snapshot.probability)})"
                if snapshot.probability is not None else "NO CURRENT SIGNAL"
            )
            embed = discord.Embed(
                title=f"🎯 Crypto Signals | {self.coin}",
                description=(
                    f"**{headline}**\n\n"
                    "Choose a coin and timeframe, then press **Get Signal** "
                    "to check the matching-market price, expiry, signal "
                    "strength, and measured forward accuracy.\n"
                    "Or **Auto-Hunt** to receive new qualified research watches "
                    "across all three coins without entering a target.\n"
                    "**Follow in DM** still edits one personal forecast card.\n\n"
                    "A model lean is not a proven profitable entry."
                ),
                color=discord.Color.orange(),
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
                    "Your personal fictional trade record is separate from "
                    "the bot's market prediction accuracy."
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
                if self.market is not None:
                    try:
                        audit = await asyncio.to_thread(
                            ForwardAccuracyMonitor(self.market).summary,
                            asset=self.coin,
                        )
                        part = audit["last_7d"][self.horizon]
                        hit = part["hit_rate"]
                        embed.add_field(
                            name=f"Bot Forecasts | {self.coin} {self.horizon}m",
                            value=(
                                f"{part['settled']} settled of "
                                f"{part['issued']} predictions in 7 days\n"
                                + (f"Direction correct: {hit:.1%}" if hit is not None
                                   else "Not enough verified outcomes")
                            ), inline=False,
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("Guided results lookup failed")
                        embed.add_field(
                            name="Bot Forecasts",
                            value="Reliable market accuracy unavailable.",
                            inline=False,
                        )
                embed.add_field(
                    name="Real-Money Profit",
                    value="**NOT PROVEN.** Practice wins are not actual payouts.",
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
                    proof = await asyncio.to_thread(
                        self.multi.report,
                        asset=self.coin, horizon_minutes=self.horizon,
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
        elif p == "help":
            embed = discord.Embed(
                title="❓ Getting Started",
                description=(
                    "1. Press **Home** whenever you feel lost.\n"
                    "2. **Sports**: choose your state, then browse games.\n"
                    "3. **Crypto**: choose a coin and time, then see a forecast.\n"
                    "4. **Practice Money**: try fake buys and sells.\n"
                    "5. **My Results**: check your practice trades.\n"
                    "6. **Advanced**: optional data freshness and model accuracy.\n\n"
                    "**PASS** means the evidence doesn't support a pick. "
                    "That is a useful answer, not a broken button."
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
        embed.set_footer(
            text="Private RivaL menu | Home • Back • Explain | No real bets",
        )
        return embed

    async def redraw(self, interaction: discord.Interaction) -> None:
        async with self._redraw_lock:
            await self._redraw_locked(interaction)

    async def _redraw_locked(
        self, interaction: discord.Interaction, *,
        _background_retry: bool = False,
    ) -> None:
        started = time.monotonic()
        self._buttons()
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
                interaction.edit_original_response(content=None, embed=embed, view=self),
                timeout=8,
            )
        except (TimeoutError, discord.HTTPException) as exc:
            edit_failed = True
            _LOG.warning("Guided menu edit failed page=%s error=%s", self.page, type(exc).__name__)
            self._reregister_view(interaction)
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

    def _reregister_view(self, interaction: discord.Interaction) -> None:
        """Re-register the rebuilt view after a failed edit.

        _buttons() detaches the previous generation's items (clear_items
        sets item.view to None). A successful edit re-registers the new
        generation via store_view, but a failed edit leaves the store
        pointing at detached items, so the next tap on the displayed
        buttons discards with no handler and no ack. Re-registering maps
        the displayed custom_ids back to attached items, healing future
        taps without a fresh menu. Best effort: never break the menu.
        """
        try:
            message = interaction.message
            store = interaction.client._connection._view_store
            if message is not None and store is not None:
                store.add_view(self, message.id)
        except Exception:  # noqa: BLE001 - healing must never break the menu
            _LOG.warning("Guided view re-register failed")

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
        if action == "explain":
            await interaction.response.send_message(
                EXPLAIN[self.page], ephemeral=True,
            )
            return
        if action in ("zip", "state", "target", "funds"):
            await interaction.response.send_modal(_Entry(self, action))
            return
        if action == "api-limits":
            application = await interaction.client.application_info()
            owner = application.owner
            if owner is None or owner.id != interaction.user.id:
                await interaction.response.send_message(
                    "Only the application owner can edit API limits.", ephemeral=True,
                )
                return
            limits = OwnerLimitStore(interaction.client.store)
            view = OwnerLimitsView(store=limits, owner_id=owner.id)
            await interaction.response.send_message(
                "API Usage & Limits: choose a provider to set your safety ceiling. "
                "This does not upgrade the provider subscription.",
                view=view, ephemeral=True,
            )
            return
        if action == "manual":
            saved = await asyncio.to_thread(
                self.location.get_saved, platform="discord",
                user_id=str(self.owner_id),
            )
            if saved is None:
                await interaction.response.send_message(
                    "Choose your betting state first.", ephemeral=True,
                )
                return
            books = set(saved.eligible_books)
            picker = await asyncio.to_thread(
                ManualParlayView, owner_id=self.owner_id,
                store=self.builder.store, eligible_books=books,
            )
            await interaction.response.send_message(
                embed=picker.render(), view=picker, ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=False)
        _LOG.info("Guided interaction acknowledged action=%s", action)
        if action == "back":
            self.page = self.back_stack.pop() if self.back_stack else "home"
            self.note = None
        elif action in ("home", "sports", "crypto", "hunt", "practice", "results",
                        "help", "advanced", "feeds", "trust"):
            self.navigate(action)
            if action == "sports":
                await self._refresh_odds()
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
        elif action in ("hunt-on", "hunt-off", "hunt-status"):
            if self.auto_hunt is None:
                self.note = "Auto-Hunt unavailable. No subscription saved."
            elif action == "hunt-on":
                _, self.note = await self.auto_hunt.subscribe(interaction.user)
            elif action == "hunt-off":
                ended = await self.auto_hunt.stop(self.owner_id)
                self.note = "Auto-Hunt stopped." if ended else "Auto-Hunt was already off."
            else:
                self.note = await self.auto_hunt.status(self.owner_id)
            self.navigate("hunt")
        elif action in ("follow", "stop"):
            try:
                if action == "stop":
                    done = await self.targets.stop(self.owner_id)
                    hunt_done = (
                        await self.auto_hunt.stop(self.owner_id)
                        if self.auto_hunt is not None else False
                    )
                    self.note = ("Private alerts stopped." if done or hunt_done else
                                 "You have no active alerts.")
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
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong. Press Home or type /rival.", ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Something went wrong. Press Home or type /rival.", ephemeral=True,
            )


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
# home menu with an honest note. A view holds at most 25 components, and the
# guided menu has 33 custom_ids, so the fallback is split across two views;
# discord.py matches by custom_id across every registered persistent view.
# ---------------------------------------------------------------------------

# Pages a stale tap may safely reopen on a fresh menu. Everything else is a
# stateful action and is never run from stale state.
_STALE_SAFE_PAGES = {
    "home": "home",
    "sports": "sports",
    "crypto": "crypto",
    "hunt": "hunt",
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
    "aggressive", "forecast", "follow", "target",
)
_STALE_BUTTONS_B = (
    "quote", "stop", "buy", "sell", "close", "funds", "proof", "trust",
    "feeds", "research", "api-limits", "hunt-on", "hunt-off", "hunt-status",
    "home", "back", "explain",
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


class _StaleCoin(discord.ui.Select["_StaleGuidedMenuBase"]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose a coin", custom_id="rival:guided:coin",
            options=[
                discord.SelectOption(label=f"{name} ({code})", value=code)
                for code, name in (
                    ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana"),
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
                for n in (5, 15)
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
    ) -> None:
        await interaction.response.defer()
        home = self.home_factory(interaction.user.id)
        if coin is not None and coin in COINS:
            home.coin = coin
        if horizon is not None and horizon in (5, 15):
            home.horizon = horizon
        if action in _STALE_SAFE_PAGES:
            home.navigate(_STALE_SAFE_PAGES[action])
        elif coin is not None or horizon is not None:
            home.navigate("crypto")
        home.note = _STALE_NOTE
        _LOG.info(
            "Stale guided menu recovered action=%s user_id=%s",
            action or f"select coin={coin} horizon={horizon}",
            interaction.user.id,
        )
        await home.redraw(interaction)


class StaleGuidedMenuViewA(_StaleGuidedMenuBase):
    """First slice of the stale-menu fallback (17 button custom_ids)."""

    def __init__(self, home_factory: Callable[[int], GuidedHome]) -> None:
        super().__init__(home_factory)
        for action in _STALE_BUTTONS_A:
            self.add_item(_StaleButton(action))


class StaleGuidedMenuViewB(_StaleGuidedMenuBase):
    """Second slice (14 buttons + coin/horizon selects)."""

    def __init__(self, home_factory: Callable[[int], GuidedHome]) -> None:
        super().__init__(home_factory)
        for action in _STALE_BUTTONS_B:
            self.add_item(_StaleButton(action))
        self.add_item(_StaleCoin())
        self.add_item(_StaleHorizon())


def build_stale_guided_menu_views(
    home_factory: Callable[[int], GuidedHome],
) -> tuple[StaleGuidedMenuViewA, StaleGuidedMenuViewB]:
    """Persistent fallback views covering every guided-menu custom_id.

    Register both with bot.add_view at startup. discord.py routes a tap on
    an expired or pre-restart menu to the matching fallback view.
    """
    return (
        StaleGuidedMenuViewA(home_factory),
        StaleGuidedMenuViewB(home_factory),
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
