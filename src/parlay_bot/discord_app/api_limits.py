"""Discord owner-only quota configuration; never displays or changes API keys."""
from __future__ import annotations

import discord

from parlay_bot.providers.owner_limits import OwnerLimit, OwnerLimitStore


class LimitModal(discord.ui.Modal, title="Change API usage limit"):
    plan = discord.ui.TextInput(label="Plan name", placeholder="free or pro", max_length=32)
    maximum = discord.ui.TextInput(label="Allowed units this billing period", placeholder="500", max_length=10)
    reserve = discord.ui.TextInput(label="Units to keep in reserve", placeholder="50", max_length=10)

    def __init__(self, *, provider: str, store: OwnerLimitStore, owner_id: int):
        super().__init__()
        self.provider = provider
        self.store = store
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the bot owner can change API limits.", ephemeral=True)
            return
        try:
            setting = OwnerLimit(
                provider=self.provider,
                plan=str(self.plan.value).strip().lower(),
                maximum_units=int(str(self.maximum.value).strip()),
                reserve_units=int(str(self.reserve.value).strip()),
            )
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(f"Nothing saved: {exc}", ephemeral=True)
            return
        self.store.save(setting)
        await interaction.response.send_message(
            f"Saved **{setting.provider}** ({setting.plan}): "
            f"{setting.maximum_units:,} allowed units, {setting.reserve_units:,} reserved. "
            "This is a safety ceiling, not a subscription upgrade. "
            "Provider-confirmed usage and entitlements still apply.",
            ephemeral=True,
        )


class OwnerLimitsView(discord.ui.View):
    def __init__(self, *, store: OwnerLimitStore, owner_id: int):
        super().__init__(timeout=900)
        self.store = store
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the bot owner can edit API budgets.", ephemeral=True)
            return False
        return True

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
