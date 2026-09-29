from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands

from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.config import Settings
from parlay_bot.discord_app.presentation import onboarding_embed, saved_location_embed
from parlay_bot.discord_app.views import LocationView, OnboardingView
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.zip_lookup import ZipStateResolver
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.storage.catalog import MarketCatalogStore
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
        self.market_catalog = MarketCatalogStore(settings.rival_db_path)
        self.parlay_builder = ParlayBuilder(
            store=self.store,
            registry=self.registry,
            catalog=self.market_catalog,
        )

        self.current_provider: SportsGameOddsClient | None = None
        self.current_collector: CurrentBoardCollector | None = None
        if settings.sportsgameodds_api_key is not None:
            self.current_provider = SportsGameOddsClient(
                settings.sportsgameodds_api_key.get_secret_value(),
                base_url=settings.sportsgameodds_base_url,
                monthly_entity_reserve=settings.sportsgameodds_monthly_entity_reserve,
            )
            self.current_collector = CurrentBoardCollector(
                client=self.current_provider,
                store=self.store,
                league_ids=self._csv_tuple(settings.rival_current_leagues),
                bookmaker_ids=self._csv_tuple(settings.rival_current_bookmakers),
                refresh_interval=timedelta(seconds=settings.rival_current_refresh_seconds),
                event_limit=settings.rival_current_event_limit,
                max_pages=settings.rival_current_max_pages,
            )

        self._register_commands()

    @staticmethod
    def _csv_tuple(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

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
                    view=OnboardingView(
                        service=self.location_service,
                        parlay_builder=self.parlay_builder,
                        current_collector=self.current_collector,
                    ),
                )
                return

            await interaction.edit_original_response(
                embed=saved_location_embed(
                    state_code=saved.state_code,
                    eligible_books=list(saved.eligible_books),
                ),
                view=LocationView(
                    service=self.location_service,
                    parlay_builder=self.parlay_builder,
                    current_collector=self.current_collector,
                ),
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
        if self.current_provider is not None:
            await self.current_provider.aclose()
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
