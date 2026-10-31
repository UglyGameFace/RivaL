"""Private owner-controlled sportsbook budgets, review and audit history.

These local ceilings cannot upgrade a provider plan or authorize billing.
Only an explicit click makes a read-only provider usage request.
"""
from __future__ import annotations

import asyncio

import discord

from parlay_bot.providers.owner_limits import OwnerLimit, OwnerLimitStore


class ConfirmLimitView(discord.ui.View):
    """No change is persisted until the owner confirms the exact values."""

    def __init__(
        self, *, store: OwnerLimitStore, owner_id: int,
        setting: OwnerLimit, previous: OwnerLimit | None,
    ) -> None:
        super().__init__(timeout=180)
        self.store = store
        self.owner_id = owner_id
        self.setting = setting
        self.previous = previous

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the application owner can approve limits.", ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm safety limit", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            await asyncio.to_thread(
                self.store.save_if_unchanged, self.setting, expected=self.previous,
            )
        except ValueError as exc:
            await interaction.response.edit_message(
                content=f"Not saved: {exc}", embed=None, view=None,
            )
            self.stop()
            return
        except Exception:  # noqa: BLE001
            await interaction.response.edit_message(
                content="Could not save API limit; no confirmation recorded.",
                embed=None, view=None,
            )
            self.stop()
            return
        await interaction.response.edit_message(
            content=(
                f"Saved **{self.setting.provider}** ({self.setting.plan}) with "
                f"{self.setting.maximum_units:,} total units and "
                f"{self.setting.reserve_units:,} protected units. "
                "The provider's real entitlement still takes precedence. "
                "Change recorded in the settings audit."
            ),
            embed=None, view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="No API limit was changed.", embed=None, view=None,
        )
        self.stop()


class LimitModal(discord.ui.Modal, title="Review API safety limit"):
    plan = discord.ui.TextInput(
        label="Your plan label (not an upgrade)",
        placeholder="free or paid", max_length=32,
    )
    maximum = discord.ui.TextInput(
        label="Your maximum units per provider period",
        placeholder="500", max_length=10,
    )
    reserve = discord.ui.TextInput(
        label="Units to protect from use", placeholder="50", max_length=10,
    )

    def __init__(self, *, provider: str, store: OwnerLimitStore, owner_id: int):
        super().__init__()
        self.provider = provider
        self.store = store
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the bot owner can change API limits.", ephemeral=True,
            )
            return
        try:
            setting = OwnerLimit(
                provider=self.provider,
                plan=str(self.plan.value).strip().lower(),
                maximum_units=int(str(self.maximum.value).strip()),
                reserve_units=int(str(self.reserve.value).strip()),
            )
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(
                f"Nothing saved: {exc}", ephemeral=True,
            )
            return
        try:
            previous = await asyncio.to_thread(self.store.get, self.provider)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message(
                "Could not read current settings; no change saved.", ephemeral=True,
            )
            return
        before = (
            f"{previous.plan}, {previous.maximum_units:,} max, "
            f"{previous.reserve_units:,} reserved"
            if previous is not None else "No owner override"
        )
        embed = discord.Embed(
            title=f"Confirm {self.provider} safety ceiling",
            description=(
                f"**Current:** {before}\n"
                f"**Proposed:** {setting.plan}, {setting.maximum_units:,} max, "
                f"{setting.reserve_units:,} reserved\n"
                "This is not an account upgrade. Provider-reported usage and "
                "permissions remain authoritative. Changes are audited."
            ),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmLimitView(
                store=self.store, owner_id=self.owner_id,
                setting=setting, previous=previous,
            ),
            ephemeral=True,
        )


class OwnerLimitsView(discord.ui.View):
    def __init__(
        self, *, store: OwnerLimitStore, owner_id: int,
        odds_provider=None, sports_provider=None,
    ) -> None:
        super().__init__(timeout=900)
        self.store = store
        self.owner_id = owner_id
        self.odds_provider = odds_provider
        self.sports_provider = sports_provider

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the bot owner can edit API budgets.", ephemeral=True,
            )
            return False
        return True

    def render(self) -> discord.Embed:
        embed = discord.Embed(
            title="API Usage & Limits",
            description=(
                "Owner safety limits only. Your provider's actual plan, remaining "
                "quota and allowed markets always take precedence. "
                "Saving requires confirmation, and every change is audited."
            ),
        )
        for provider, label, unit in (
            ("oddspapi", "OddsPapi", "requests"),
            ("sportsgameodds", "SportsGameOdds", "entities"),
        ):
            configured = self.store.get(provider)
            history = self.store.history(provider, limit=1)
            if configured is None:
                line = "No custom cap set; provider quota and built-in reserve still apply."
            else:
                line = (
                    f"Plan label: **{configured.plan}**\n"
                    f"Owner ceiling: **{configured.maximum_units:,} {unit}**\n"
                    f"Protected: **{configured.reserve_units:,} {unit}**"
                )
            if history:
                line += "\nLast owner change: " + history[0]["changed_at"][:16] + " UTC"
            embed.add_field(name=label, value=line, inline=False)
        embed.set_footer(
            text="Check Provider Usage is on-demand; viewing settings uses no API quota."
        )
        return embed

    @discord.ui.button(label="OddsPapi limits", style=discord.ButtonStyle.primary)
    async def oddspapi(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            LimitModal(provider="oddspapi", store=self.store, owner_id=self.owner_id)
        )

    @discord.ui.button(label="SportsGameOdds limits", style=discord.ButtonStyle.secondary)
    async def sportsgameodds(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            LimitModal(provider="sportsgameodds", store=self.store, owner_id=self.owner_id)
        )

    @discord.ui.button(label="Review saved settings", style=discord.ButtonStyle.secondary)
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed = await asyncio.to_thread(self.render)
        except Exception:  # noqa: BLE001
            await interaction.followup.send(
                "Settings cannot be read; nothing was changed.", ephemeral=True,
            )
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Check provider usage", style=discord.ButtonStyle.secondary)
    async def usage(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Only an explicit owner click checks the providers' account endpoints."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        details = discord.Embed(
            title="Provider reported usage",
            description=(
                "These are read-only account reports. An API request may itself "
                "count against your provider's request allowance."
            ),
        )
        for title, provider, kind in (
            ("OddsPapi", self.odds_provider, "oddspapi"),
            ("SportsGameOdds", self.sports_provider, "sportsgameodds"),
        ):
            if provider is None:
                value = "Not configured / sports background service inactive."
            else:
                try:
                    if kind == "oddspapi":
                        report = await asyncio.wait_for(provider.get_account(), timeout=6)
                        value = (
                            f"Provider requests: {report.request_count:,} / "
                            f"{report.request_limit:,}\n"
                            f"Provider remaining: {report.remaining_requests:,}"
                        )
                    else:
                        report = await asyncio.wait_for(provider.get_usage(), timeout=6)
                        remaining = report.monthly_remaining_entities
                        if remaining is None:
                            value = "Unknown monthly provider allowance; event requests blocked."
                        else:
                            value = (
                                f"Provider plan: {report.tier or 'not reported'}\n"
                                f"Entities used: {report.monthly_current_entities:,} / "
                                f"{report.monthly_max_entities:,}\n"
                                f"Provider remaining: {remaining:,}"
                            )
                except Exception:  # noqa: BLE001
                    value = "Provider usage unavailable; no additional entitlement assumed."
            details.add_field(name=title, value=value, inline=False)
        await interaction.followup.send(embed=details, ephemeral=True)
