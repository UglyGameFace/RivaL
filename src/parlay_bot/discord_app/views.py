from __future__ import annotations

import logging
import sqlite3

import discord
import httpx

from parlay_bot.discord_app.presentation import location_embed, onboarding_embed
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService, OnboardingLocation
from parlay_bot.jurisdiction.zip_lookup import ZipLookupError

_LOG = logging.getLogger(__name__)


async def _finish_location(
    interaction: discord.Interaction,
    location: OnboardingLocation,
    service: LocationOnboardingService,
) -> None:
    await interaction.edit_original_response(
        embed=location_embed(location),
        view=LocationView(service=service),
    )


async def _modal_error(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(content=message, embed=None, view=None)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _log_interaction_error(
    *,
    error: Exception,
    user_id: int,
    custom_id: str | None = None,
) -> None:
    _LOG.error(
        "RivaL interaction failed custom_id=%s user=%s",
        custom_id,
        user_id,
        exc_info=(type(error), error, error.__traceback__),
    )


class RivalView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        _log_interaction_error(
            error=error,
            user_id=interaction.user.id,
            custom_id=getattr(item, "custom_id", None),
        )
        message = "RivaL hit an unexpected error. Your setting was not intentionally changed."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class RivalModal(discord.ui.Modal):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        _log_interaction_error(error=error, user_id=interaction.user.id)
        await _modal_error(
            interaction,
            "RivaL hit an unexpected error. Your location was not intentionally changed.",
        )


class ZipModal(RivalModal, title="Set location with ZIP"):
    zip_code = discord.ui.TextInput(
        label="ZIP code",
        placeholder="06001",
        min_length=5,
        max_length=5,
        required=True,
    )

    def __init__(self, *, service: LocationOnboardingService) -> None:
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            location = await self.service.choose_zip(
                platform="discord",
                user_id=str(interaction.user.id),
                zip_code=str(self.zip_code.value),
            )
        except ZipLookupError as exc:
            await _modal_error(interaction, f"That ZIP could not be used: {exc}")
            return
        except (httpx.HTTPError, sqlite3.Error):
            _LOG.exception("RivaL ZIP onboarding dependency failed user=%s", interaction.user.id)
            await _modal_error(
                interaction,
                "RivaL could not save that location right now. No ZIP was stored.",
            )
            return

        await _finish_location(interaction, location, self.service)


class StateModal(RivalModal, title="Choose betting state"):
    state = discord.ui.TextInput(
        label="State or abbreviation",
        placeholder="Connecticut or CT",
        min_length=2,
        max_length=32,
        required=True,
    )

    def __init__(self, *, service: LocationOnboardingService) -> None:
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            location = self.service.choose_state(
                platform="discord",
                user_id=str(interaction.user.id),
                state=str(self.state.value),
            )
        except ValueError:
            await _modal_error(
                interaction,
                "I couldn't match that to a U.S. state or Washington, D.C. "
                "Try the full state name or two-letter abbreviation.",
            )
            return
        except sqlite3.Error:
            _LOG.exception("RivaL state onboarding database failed user=%s", interaction.user.id)
            await _modal_error(interaction, "RivaL could not save that state right now.")
            return

        await _finish_location(interaction, location, self.service)


class OnboardingView(RivalView):
    def __init__(self, *, service: LocationOnboardingService) -> None:
        super().__init__(timeout=900)
        self.service = service

    @discord.ui.button(
        label="Enter ZIP",
        style=discord.ButtonStyle.primary,
        emoji="📍",
        custom_id="rival:onboarding:zip",
    )
    async def enter_zip(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(ZipModal(service=self.service))

    @discord.ui.button(
        label="Choose State",
        style=discord.ButtonStyle.secondary,
        emoji="🗺️",
        custom_id="rival:onboarding:state",
    )
    async def choose_state(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(StateModal(service=self.service))


class LocationView(RivalView):
    def __init__(self, *, service: LocationOnboardingService) -> None:
        super().__init__(timeout=900)
        self.service = service

    @discord.ui.button(
        label="Change Location",
        style=discord.ButtonStyle.secondary,
        emoji="📍",
        custom_id="rival:location:change",
    )
    async def change_location(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            embed=onboarding_embed(),
            view=OnboardingView(service=self.service),
        )

    @discord.ui.button(
        label="Refresh Books",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="rival:location:refresh",
    )
    async def refresh_books(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        saved = self.service.get_saved(
            platform="discord",
            user_id=str(interaction.user.id),
        )
        if saved is None:
            await interaction.response.edit_message(
                embed=onboarding_embed(),
                view=OnboardingView(service=self.service),
            )
            return

        await interaction.response.edit_message(
            embed=location_embed(saved),
            view=LocationView(service=self.service),
        )
