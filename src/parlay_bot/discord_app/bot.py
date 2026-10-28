from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from parlay_bot.archive.runtime import ColdArchiveRuntime, build_cold_archive_runtime
from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.config import Settings
from parlay_bot.discord_app.data_dashboard import MarketDataDashboard
from parlay_bot.discord_app.presentation import onboarding_embed, saved_location_embed
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.discord_app.views import LocationView, OnboardingView
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.zip_lookup import ZipStateResolver
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.perpetuals.collector import PerpetualMarketCollector
from parlay_bot.perpetuals.gemini_research import GeminiResearch
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.storage.catalog import MarketCatalogStore
from parlay_bot.storage.hot import PostgresHotStore, RelationalHotStore, SQLiteHotStore

_LOG = logging.getLogger(__name__)


def build_hot_store(settings: Settings) -> RelationalHotStore:
    if settings.rival_storage_backend == "sqlite":
        return SQLiteHotStore(settings.rival_db_path)

    if settings.rival_database_url is None:
        raise RuntimeError(
            "RIVAL_DATABASE_URL is required when RIVAL_STORAGE_BACKEND=postgres"
        )
    return PostgresHotStore(settings.rival_database_url.get_secret_value())


class RivalDiscordClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        super().__init__(intents=discord.Intents.none())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)

        self.store = build_hot_store(settings)
        self.store.initialize()
        _LOG.info(
            "RivaL operational storage initialized (backend=%s)",
            self.store.database.backend,
        )
        self.registry = JurisdictionRegistry(verified_us_sportsbooks())
        self.zip_resolver = ZipStateResolver()
        self.location_service = LocationOnboardingService(
            store=self.store,
            registry=self.registry,
            zip_resolver=self.zip_resolver,
        )
        self.market_catalog = MarketCatalogStore(self.store)
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

        self.archive_runtime: ColdArchiveRuntime | None = build_cold_archive_runtime(
            settings,
            store=self.store,
        )
        self._archive_task: asyncio.Task[None] | None = None

        # No extra account is needed: two public, read-only feeds start
        # automatically with the existing PostgreSQL Discord deployment.
        self.perp_collector: PerpetualMarketCollector | None = None
        if (
            settings.rival_perp_collection_enabled
            and settings.rival_storage_backend == "postgres"
        ):
            self.perp_collector = PerpetualMarketCollector(
                store=self.store,
                assets=self._csv_tuple(settings.rival_perp_collection_assets),
                bars=settings.rival_perp_collection_bars,
            )
        self._perp_task: asyncio.Task[None] | None = None
        self.shadow_monitor: ShadowLearningMonitor | None = (
            ShadowLearningMonitor(self.store)
            if self.perp_collector is not None and settings.rival_shadow_learning_enabled
            else None
        )
        self._shadow_task: asyncio.Task[None] | None = None
        secret = settings.gemini_api_key or settings.google_api_key
        self.gemini_research: GeminiResearch | None = (
            GeminiResearch(
                self.perp_collector.market,
                api_key=secret.get_secret_value() if secret is not None else None,
                model=settings.rival_gemini_model,
                daily_max=settings.rival_gemini_daily_max,
                cooldown_seconds=settings.rival_gemini_cooldown_seconds,
            )
            if self.perp_collector is not None else None
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

        @self.tree.command(
            name="rival-data",
            description="Open the private RivaL market data dashboard.",
        )
        async def rival_data(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            collector = self.perp_collector
            dashboard = MarketDataDashboard(
                owner_id=interaction.user.id,
                market=collector.market if collector is not None else None,
                venues=tuple(collector.providers) if collector is not None else (),
                assets=collector.assets if collector is not None else (),
                interval_seconds=self.settings.rival_perp_collection_seconds,
                enabled=collector is not None,
            )
            await interaction.edit_original_response(
                embed=await dashboard.render(), view=dashboard,
            )

        @self.tree.command(
            name="rival-research",
            description="Private mathematical history and optional Gemini analysis.",
        )
        async def rival_research(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            collector = self.perp_collector
            dashboard = ResearchDashboard(
                owner_id=interaction.user.id,
                market=collector.market if collector is not None else None,
                ai=self.gemini_research,
                venues=tuple(collector.providers) if collector is not None else (),
                shadow_enabled=self.shadow_monitor is not None,
            )
            await interaction.edit_original_response(
                embed=await dashboard.render(), view=dashboard,
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
        else:
            synced = await self.tree.sync()
            _LOG.info("RivaL synced %d global command(s)", len(synced))

        if self.perp_collector is not None and self._perp_task is None:
            _LOG.info(
                "RivaL perp automatic collector ENABLED venues=%s "
                "assets=%s interval=%ds backend=postgres",
                ",".join(self.perp_collector.providers),
                ",".join(self.perp_collector.assets),
                self.settings.rival_perp_collection_seconds,
            )
            self._perp_task = asyncio.create_task(
                self._perp_loop(), name="rival-perp-auto-collector"
            )

        if self.shadow_monitor is not None and self._shadow_task is None:
            _LOG.info(
                "RivaL automatic shadow learning ENABLED interval=%ds "
                "max_streams=%d model=shadow_only",
                self.settings.rival_shadow_eval_seconds, 10,
            )
            self._shadow_task = asyncio.create_task(
                self._shadow_loop(), name="rival-shadow-learning"
            )

        if self.archive_runtime is not None and self._archive_task is None:
            self._archive_task = asyncio.create_task(
                self._archive_loop(),
                name="rival-cold-archive",
            )

    async def _perp_loop(self) -> None:
        await self.wait_until_ready()
        assert self.perp_collector is not None
        _LOG.info("RivaL perp automatic collector RUNNING")
        while not self.is_closed():
            try:
                await self.perp_collector.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - collector never crashes Discord
                _LOG.exception("RivaL perp automatic collection cycle failed")
            await asyncio.sleep(self.settings.rival_perp_collection_seconds)

    async def _shadow_loop(self) -> None:
        await self.wait_until_ready()
        assert self.shadow_monitor is not None
        while not self.is_closed():
            try:
                await asyncio.to_thread(self.shadow_monitor.run_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - shadow research cannot kill Discord
                _LOG.exception("RivaL automatic shadow evaluation failed safely")
            await asyncio.sleep(self.settings.rival_shadow_eval_seconds)

    async def _archive_loop(self) -> None:
        await self.wait_until_ready()
        assert self.archive_runtime is not None

        while not self.is_closed():
            try:
                cutoff = datetime.now(UTC) - timedelta(
                    minutes=self.settings.rival_archive_closed_state_minutes
                )
                completed = await self.archive_runtime.service.archive_ready(
                    cutoff=cutoff,
                    max_rows=self.settings.rival_archive_batch_rows,
                    max_batches=self.settings.rival_archive_max_batches,
                )
                if completed:
                    _LOG.info(
                        "RivaL cold archive completed %d batch(es)",
                        len(completed),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background archive must not kill Discord
                _LOG.exception("RivaL cold archive cycle failed safely")

            await asyncio.sleep(self.settings.rival_archive_run_seconds)

    async def close(self) -> None:
        if self._shadow_task is not None:
            self._shadow_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._shadow_task
            self._shadow_task = None
        if self._perp_task is not None:
            self._perp_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._perp_task
            self._perp_task = None
        if self.perp_collector is not None:
            await self.perp_collector.aclose()
        if self.gemini_research is not None:
            await self.gemini_research.aclose()

        if self._archive_task is not None:
            self._archive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._archive_task
            self._archive_task = None

        if self.archive_runtime is not None:
            await self.archive_runtime.drive.aclose()
        if self.current_provider is not None:
            await self.current_provider.aclose()
        await self.zip_resolver.aclose()
        self.store.close()
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
