from __future__ import annotations

import logging

import discord
from discord import app_commands

from parlay_bot.config import Settings
from parlay_bot.discord_app.presentation import onboarding_embed, saved_location_embed
from parlay_bot.discord_app.views import LocationView, OnboardingView
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.zip_lookup import ZipStateResolver
from parlay_bot.storage.hot import SQLiteHotStore

_LOG = logging.getLogger(__name__)


class RivalDiscordClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        super().__init__(intents=discord.Intents.none())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)

        self.store = SQLiteHotStore(settings.rival_db_path)
        self.store.initialize()
        self.registry = JurisdictionRegistry(verified_us_sportsbooks())
        self.zip_resolver = ZipStateResolver()
        self.location_service = LocationOnboardingService(
            store=self.store,
            registry=self.registry,
            zip_resolver=self.zip_resolver,
        )

        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="rival",
            description="Open your private RivaL sports odds dashboard.",
        )
        async def rival(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)

            saved = self.location_service.get_saved(
                platform="discord",
                user_id=str(interaction.user.id),
            )
            if saved is None:
                await interaction.edit_original_response(
                    embed=onboarding_embed(),
                    view=OnboardingView(service=self.location_service),
                )
                return

            await interaction.edit_original_response(
                embed=saved_location_embed(
                    state_code=saved.state_code,
                    eligible_books=list(saved.eligible_books),
                ),
                view=LocationView(service=self.location_service),
            )

    async def setup_hook(self) -> None:
        if self.settings.rival_dev_guild_id is not None:
            guild = discord.Object(id=self.settings.rival_dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            _LOG.info(
                "RivaL synced %d command(s) to development guild %s",
                len(synced),
                guild.id,
            )
            return

        synced = await self.tree.sync()
        _LOG.info("RivaL synced %d global command(s)", len(synced))

    async def close(self) -> None:
        await self.zip_resolver.aclose()
        await super().close()


def run_discord_bot(settings: Settings | None = None) -> None:
    configured = settings or Settings()
    if configured.discord_token is None:
        raise RuntimeError("DISCORD_TOKEN is required to run RivaL")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client = RivalDiscordClient(configured)
    client.run(configured.discord_token.get_secret_value(), log_handler=None)
