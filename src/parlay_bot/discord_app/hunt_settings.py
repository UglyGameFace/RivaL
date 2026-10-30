"""Small owner-only Discord settings for live trend and Polymarket alerts.

Three direct selectors (coins, window, direction) and plain-language
buttons. No Discord elevated server permissions or separate website.
"""
from __future__ import annotations

import discord

from parlay_bot.discord_app.auto_hunt import AutoHuntService

MARKET_COINS = ("BTC", "ETH", "SOL", "XRP")
MARKET_HORIZONS = (5, 15)


class _Choose(discord.ui.Select["HuntSettingsView"]):
    def __init__(self, field: str, choices: tuple, selected: object,
                 *, many: bool = False, row: int = 0) -> None:
        self.field = field
        items = selected if many else (selected,)
        super().__init__(
            placeholder={"assets": "Choose coins", "horizons": "Choose time windows",
                         "side": "Which outcome?"}[field],
            min_values=1, max_values=len(choices) if many else 1,
            options=[
                discord.SelectOption(
                    label=(f"{entry} minutes" if field == "horizons" else
                           {"both": "Either Up or Down", "up": "Up only",
                            "down": "Down only"}.get(str(entry), str(entry))),
                    value=str(entry), default=entry in items,
                )
                for entry in choices
            ],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.set_value(
            interaction, self.field,
            tuple(int(x) for x in self.values) if self.field == "horizons"
            else tuple(self.values) if self.field == "assets"
            else self.values[0],
        )


class _Preference(discord.ui.Button["HuntSettingsView"]):
    def __init__(self, label: str, action: str, row: int):
        super().__init__(label=label, row=row, style=discord.ButtonStyle.secondary)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.act(interaction, self.action)


class HuntSettingsView(discord.ui.View):
    def __init__(self, owner_id: int, service: AutoHuntService, prefs: dict,
                 *, page: str = "markets") -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.service = service
        self.prefs = prefs
        self.page = page
        if page == "markets":
            self.add_item(_Choose(
                "assets", MARKET_COINS, prefs["assets"], many=True, row=0,
            ))
            self.add_item(_Choose(
                "horizons", MARKET_HORIZONS, prefs["horizons"], many=True, row=1,
            ))
            self.add_item(_Choose(
                "side", ("both", "up", "down"), prefs["side"], row=2,
            ))
            self.add_item(_Preference(
                f"Max Ask: {prefs['max_ask_pct']}%", "max_ask_pct", 3,
            ))
            self.add_item(_Preference(
                f"Min Time: {prefs['min_remaining_seconds']//60}m"
                if prefs["min_remaining_seconds"] % 60 == 0
                else f"Min Time: {prefs['min_remaining_seconds']}s",
                "min_remaining_seconds", 3,
            ))
            self.add_item(_Preference(
                "Daily cap: " + (
                    str(prefs["daily_limit"]) if prefs["daily_limit"] else "Unlimited"
                ), "daily_limit", 3,
            ))
            self.add_item(_Preference("Direction Alerts", "shifts", 4))
            self.add_item(_Preference("Done", "done", 4))
        else:
            self.add_item(_Preference(
                "Live Price Shifts: " + (
                    "ON" if prefs["live_shifts"] else "OFF"),
                "live_shifts", 0,
            ))
            self.add_item(_Preference(
                "New Model Reversals: " + (
                    "ON" if prefs["direction_changes"] else "OFF"),
                "direction_changes", 1,
            ))
            self.add_item(_Preference("Market Filters", "markets", 3))
            self.add_item(_Preference("Done", "done", 4))

    def render(self) -> str:
        assets = ", ".join(self.prefs["assets"])
        horizons = ", ".join(f"{x}m" for x in self.prefs["horizons"])
        if self.page == "markets":
            return (
                "**Customize Auto-Hunt**\n"
                f"Coins: **{assets}** • Windows: **{horizons}**\n"
                f"Polymarket sides: **{self.prefs['side']}** • "
                f"Maximum ask: **{self.prefs['max_ask_pct']}%** • "
                f"Time remaining: **{self.prefs['min_remaining_seconds']}s**\n"
                f"Daily limit per alert type: **"
                f"{self.prefs['daily_limit'] or 'Unlimited'}**\n\n"
                "Polymarket has listed BTC/ETH/SOL 5m/15m markets; XRP requires its own live market feed. "
                "The max ask is a PRICE filter (not a probability "
                "estimate); it does not establish profitable odds. "
                "Select coins and windows independently, then press "
                "Direction Alerts to choose automatic change notifications."
            )
        return (
            "**Direction Alerts**\n"
            f"Live price shifts: **{'ON' if self.prefs['live_shifts'] else 'OFF'}**\n"
            f"New model reversals: "
            f"**{'ON' if self.prefs['direction_changes'] else 'OFF'}**\n\n"
            "Live shifts sample **two exchange WebSockets every 5 seconds** "
            "and require a confirmed momentum reversal. Model reversals "
            "come from independently issued completed-candle forecasts, "
            "not chart ticks. Both are research-only, never guaranteed "
            "winning predictions. They use your selected coins."
        )

    async def redraw(self, interaction: discord.Interaction) -> None:
        fresh = HuntSettingsView(self.owner_id, self.service, self.prefs,
                                 page=self.page)
        await interaction.edit_original_response(content=fresh.render(), view=fresh)

    async def set_value(self, interaction: discord.Interaction,
                        field: str, value: object) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only your own alerts can be changed.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            self.prefs = await self.service.update_preference(
                self.owner_id, field, value,
            )
        except (ValueError, discord.HTTPException, TimeoutError):
            await interaction.followup.send(
                "Setting not saved. Check your DMs or choose a supported option.",
                ephemeral=True,
            )
        await self.redraw(interaction)

    async def act(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only your own alerts can be changed.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        if action == "done":
            await interaction.edit_original_response(
                content="Auto-Hunt preferences saved. Use /rival → My Alerts "
                        "to check whether each source is ON.",
                view=None,
            )
            return
        if action in ("shifts", "markets"):
            self.page = "shifts" if action == "shifts" else "markets"
        else:
            old = self.prefs[action]
            new = (
                (0 if old else 1) if action in ("live_shifts", "direction_changes")
                else {100: 75, 75: 50, 50: 25, 25: 100}[old]
                if action == "max_ask_pct"
                else {0: 12, 12: 24, 24: 48, 48: 0}[old]
                if action == "daily_limit"
                else {90: 180, 180: 300, 300: 90}[old]
            )
            try:
                self.prefs = await self.service.update_preference(
                    self.owner_id, action, new,
                )
            except (ValueError, discord.HTTPException, TimeoutError):
                await interaction.followup.send(
                    "Could not enable this alert. Confirm DMs are allowed.",
                    ephemeral=True,
                )
        await self.redraw(interaction)
