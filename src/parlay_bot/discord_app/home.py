"""Private, guided RivaL home for ordinary users, without losing advanced features."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

import discord

from parlay_bot.discord_app.opportunities import crypto_desk_embed, sportsbook_desk_embed
from parlay_bot.discord_app.presentation import onboarding_embed, parlay_embed
from parlay_bot.discord_app.targeting import target_embed, target_snapshot
from parlay_bot.jurisdiction.zip_lookup import ZipLookupError
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import QuoteUnavailable, quote_embed

if TYPE_CHECKING:
    from parlay_bot.collectors.current import CurrentBoardCollector
    from parlay_bot.discord_app.targeting import DirectionTargetService
    from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
    from parlay_bot.parlays.builder import ParlayBuilder
    from parlay_bot.perpetuals.live_quotes import PublicBestQuoteClient
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.perpetuals.paper_ledger import PaperLedger
    from parlay_bot.perpetuals.profit_proof import ProspectiveQuoteProfitMonitor

_LOG = logging.getLogger(__name__)
COINS = ("BTC", "ETH", "SOL")
EXPLAIN = {
    "home": "Choose an area. Your menu is private, and no real wagers are placed.",
    "sports": "Sports odds show possible payouts, not guaranteed wins. PASS means no reliable recommendation is available. Set your state to filter sportsbooks.",
    "slip": "A parlay combines picks. All legs must win for the parlay to win. No real wager is placed here.",
    "crypto": "Choose a coin and timeframe. UP/DOWN is a forecast, not permission to trade. A price target is a number to watch, not a predicted certainty.",
    "forecast": "WAIT means there is no fresh, trustworthy forecast. A correct direction could still lose money after trading costs.",
    "quote": "Ask is the price you might buy at; bid is the price you might sell at. These quotes are not guaranteed fills.",
    "practice": "Everything in Practice Money is fictional. Buying means you expect a rise; selling means you expect a fall. Fake gains never count as proven real profit.",
    "results": "Practice results are your own fake trades. Model accuracy is measured separately from later verified market outcomes. Real profit is not yet proven.",
    "help": "Start at Home. For sports, select your state. For crypto, choose a coin and press See Forecast. To learn without real money, open Practice Money.",
    "advanced": "Advanced research shows models, provider diagnostics, costs, accuracy and historical data. No real bets or orders are placed.",
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
        await self.view.act(interaction, self.action)


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


class GuidedHome(discord.ui.View):
    """One private menu; real actions are wired to existing authoritative services."""

    def __init__(
        self, *, owner_id: int, location: LocationOnboardingService,
        builder: ParlayBuilder, collector: CurrentBoardCollector | None,
        market: PerpetualMarketStore | None, targets: DirectionTargetService,
        paper: PaperLedger, quotes: PublicBestQuoteClient,
        profit: ProspectiveQuoteProfitMonitor | None,
        research_factory: Callable[[int], discord.ui.View] | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.location = location
        self.builder = builder
        self.collector = collector
        self.market = market
        self.targets = targets
        self.paper = paper
        self.quotes = quotes
        self.profit = profit
        self.research_factory = research_factory
        self.page = "home"
        self.back_stack: list[str] = []
        self.coin = "BTC"
        self.horizon = 5
        self.risk = RiskMode.BALANCED
        self.slip = None
        self.note: str | None = None
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
            self._add("Build 3-Pick Slip", "slip", 1, "🎟️", True)
        elif p == "slip":
            self._add("Lower Risk", "lower", 0, "🛡️")
            self._add("Balanced", "balanced", 0, "⚖️")
            self._add("Higher Payout", "aggressive", 0, "🎯")
            self._add("Try Again", "slip", 1, "🔄")
        elif p in ("crypto", "forecast", "quote"):
            self.add_item(_Coin(self.coin))
            self.add_item(_Horizon(self.horizon))
            self._add("See Forecast", "forecast", 2, "📈", True)
            self._add("Follow in DM", "follow", 2, "🔔")
            self._add("Set Price Target", "target", 2, "🎯")
            self._add("See Buy/Sell Prices", "quote", 3, "💱")
            self._add("Stop My Alerts", "stop", 3, "🔕")
        elif p == "practice":
            self.add_item(_Coin(self.coin))
            self._add("Practice Buy 25", "buy", 1, "📈", True)
            self._add("Practice Sell 25", "sell", 1, "📉")
            self._add("Close Fake Trade", "close", 2, "✅")
            self._add("Set Fake Balance", "funds", 2, "💵")
            self._add("See My Results", "results", 3, "📊")
        elif p == "results":
            self.add_item(_Coin(self.coin))
            self.add_item(_Horizon(self.horizon))
            self._add("Check Profit Evidence", "proof", 2, "🔍")
            self._add("View Research Details", "research", 2, "📊")
        elif p == "help":
            self._add("Start with Sports", "sports", 0, "🏆")
            self._add("Try Crypto", "crypto", 0, "📈")
            self._add("Practice Safely", "practice", 1, "💵")
        elif p == "advanced":
            self._add("Open Technical Research", "research", 0, "🧪")
            self._add("See Accuracy", "results", 0, "📊")
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

    async def _refresh_odds(self) -> None:
        if self.collector is None:
            return
        try:
            await asyncio.wait_for(self.collector.refresh_if_due(), timeout=15)
        except Exception:  # noqa: BLE001
            _LOG.warning("Beginner sports refresh failed, stale cache filtered", exc_info=True)

    async def render(self) -> discord.Embed:
        p = self.page
        if p == "home":
            embed = discord.Embed(
                title="RivaL | Betting Genie",
                description=(
                    "**Welcome! What would you like to do?**\n"
                    "🏆 **Sports & Odds**: Find games and compare prices.\n"
                    "📈 **Predict Crypto**: See BTC, ETH or SOL forecasts.\n"
                    "💵 **Practice Money**: Try fake funds, no deposits.\n"
                    "📊 **My Results**: See wins and losses.\n"
                    "❓ **Help Me Start**: Simple instructions.\n\n"
                    "**No real bets or orders are placed.**"
                ), color=discord.Color.blurple(),
            )
        elif p == "sports":
            saved = await asyncio.to_thread(
                self.location.get_saved, platform="discord",
                user_id=str(self.owner_id),
            )
            if saved is None:
                embed = onboarding_embed()
                embed.title = "🏆 Sports & Odds | First Step"
                embed.description = (
                    "Choose your state or enter a ZIP code above to see "
                    "relevant sportsbooks. RivaL saves your state, not your ZIP."
                )
            else:
                embed = await asyncio.to_thread(
                    sportsbook_desk_embed, self.builder,
                    user_id=str(self.owner_id),
                )
                embed.title = "🏆 Sports & Odds"
                embed.add_field(
                    name="Your Area", value=saved.state_name, inline=False,
                )
        elif p == "slip":
            if self.slip is None:
                embed = discord.Embed(
                    title="🎟️ Sports Slip",
                    description="PASS: No qualifying available prices. No wager made.",
                    color=discord.Color.orange(),
                )
            else:
                embed = parlay_embed(self.slip)
                embed.title = "🎟️ Your Sports Slip"
                embed.description = (
                    (embed.description or "")
                    + "\n\n**No wager was placed. All legs must win.**"
                )
        elif p == "crypto":
            embed = await asyncio.to_thread(
                crypto_desk_embed, self.market, asset=self.coin,
            )
            embed.title = f"📈 Predict Crypto | {self.coin}"
            embed.add_field(
                name="Start Here",
                value=(
                    "Choose a coin and timeframe above, then press "
                    "**See Forecast**. Press **Follow in DM** for "
                    "automatic private updates."
                ), inline=False,
            )
        elif p == "forecast":
            prediction = await asyncio.to_thread(
                target_snapshot, self.market,
                asset=self.coin, horizon_minutes=self.horizon,
            )
            embed = target_embed(prediction)
            embed.title = f"📈 {self.coin} | {self.horizon}-Minute Forecast"
        elif p == "quote":
            price = await self.quotes.get(self.coin)
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
        elif p == "help":
            embed = discord.Embed(
                title="❓ Getting Started",
                description=(
                    "1. Press **Home** whenever you feel lost.\n"
                    "2. **Sports**: choose your state, then browse games.\n"
                    "3. **Crypto**: choose a coin and time, then see a forecast.\n"
                    "4. **Practice Money**: try fake buys and sells.\n"
                    "5. **My Results**: check wins, losses and accuracy.\n\n"
                    "**PASS** means the evidence doesn't support a pick. "
                    "That is a useful answer, not a broken button."
                ), color=discord.Color.blurple(),
            )
        else:
            embed = discord.Embed(
                title="⚙️ Advanced Tools",
                description=(
                    "**Optional, for experienced users.**\n"
                    "See mathematical models, historical data, costs and "
                    "source reliability in Advanced Research.\n"
                    "You can also use /rival-data, /rival-quote, "
                    "/rival-target-proof, /rival-profit-proof, and "
                    "/rival-sport-proof.\n"
                    "**None of these place real wagers.**"
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
        self._buttons()
        try:
            embed = await self.render()
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
        await interaction.edit_original_response(
            content=None, embed=embed, view=self,
        )

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        if action == "explain":
            await interaction.response.send_message(
                EXPLAIN[self.page], ephemeral=True,
            )
            return
        if action in ("zip", "state", "target", "funds"):
            await interaction.response.send_modal(_Entry(self, action))
            return
        await interaction.response.defer()
        if action == "back":
            self.page = self.back_stack.pop() if self.back_stack else "home"
            self.note = None
        elif action in ("home", "sports", "crypto", "practice", "results",
                        "help", "advanced"):
            self.navigate(action)
            if action == "sports":
                await self._refresh_odds()
        elif action == "refresh":
            await self._refresh_odds()
            self.note = "Checked the current sportsbook data."
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
        elif action in ("follow", "stop"):
            try:
                if action == "stop":
                    done = await self.targets.stop(self.owner_id)
                    self.note = ("Private alerts stopped." if done else
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
            if self.profit is None:
                self.note = "Live profit verification unavailable. NOT PROVEN."
            else:
                try:
                    result = await asyncio.to_thread(
                        self.profit.proof,
                        asset=self.coin, horizon_minutes=self.horizon,
                    )
                    self.note = (
                        f"{result.verified_quote_exits} of "
                        f"{result.eligible_forecast_windows} forecast windows "
                        "have complete observed entry/exit quotes. "
                        "Actual real-money profit NOT PROVEN."
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("Guided profitability unavailable")
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
