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
from parlay_bot.discord_app.opportunities import (\n    crypto_desk_embed,\n    sportsbook_desk_embed,\n)\nfrom parlay_bot.discord_app.presentation import onboarding_embed
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.discord_app.views import LocationView, OnboardingView
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.zip_lookup import ZipStateResolver
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.bitget_history_recovery import BitgetHistoryRecovery
from parlay_bot.perpetuals.collector import PerpetualMarketCollector
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.gemini_research import GeminiResearch
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
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


def next_collection_delay(
    now: datetime, interval_seconds: int, *, settle_seconds: int = 15,
) -> float:
    """Schedule collection after the next UTC candle boundary, without drift.

    The existing collector is still read-only, sequential and rate limited.
    Scheduling is anchored to UTC, not to how long 30 venue calls happened
    to take during the previous poll.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    if interval_seconds < 60 or not 0 <= settle_seconds < interval_seconds:
        raise ValueError("invalid bar schedule")
    timestamp = now.timestamp()
    boundary = (int(timestamp // interval_seconds) + 1) * interval_seconds
    return max(0.01, boundary + settle_seconds - timestamp)


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

        # Enable archive and perpetual cold copies automatically once the
        # private bot-owned Drive OAuth + boundary IDs are fully configured.
        # Partial/missing credentials never interrupt Discord or imply success.
        has_drive_config = all((
            settings.rival_drive_root_folder_id,
            settings.rival_drive_history_folder_id,
            settings.rival_drive_manifests_folder_id,
            settings.rival_drive_staging_folder_id,
            settings.rival_drive_oauth_client_id,
            settings.rival_drive_oauth_client_secret,
            settings.rival_drive_oauth_refresh_token,
        ))
        archive_settings = settings
        if (
            settings.rival_archive_autostart_when_configured
            and has_drive_config
            and not settings.rival_archive_enabled
        ):
            archive_settings = settings.model_copy(update={
                "rival_archive_enabled": True,
                "rival_perp_archive_enabled": True,
            })
            _LOG.info("RivaL private Drive archiving auto-configured; "
                      "remote authorization remains subject to actual upload checks")
        self._sports_task: asyncio.Task[None] | None = None
        self.archive_runtime: ColdArchiveRuntime | None = build_cold_archive_runtime(
            archive_settings,
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
        self.history_auditor: SixMonthQualityAudit | None = (
            SixMonthQualityAudit(self.perp_collector.market)
            if self.perp_collector is not None and settings.rival_history_audit_enabled
            else None
        )
        self.history_recovery: GateHistoryRecovery | None = (
            GateHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.kucoin_history_recovery: KucoinHistoryRecovery | None = (
            KucoinHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.bitget_history_recovery: BitgetHistoryRecovery | None = (
            BitgetHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.okx_history_recovery: OkxHistoryRecovery | None = (
            OkxHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self._history_task: asyncio.Task[None] | None = None
        self.shadow_monitor: ShadowLearningMonitor | None = (
            ShadowLearningMonitor(self.store)
            if self.perp_collector is not None and settings.rival_shadow_learning_enabled
            else None
        )
        self.forward_audit: ForwardAccuracyMonitor | None = (
            ForwardAccuracyMonitor(self.perp_collector.market)
            if self.shadow_monitor is not None and self.perp_collector is not None
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
        self.autonomous: AutonomousResearch | None = (
            AutonomousResearch(
                self.perp_collector.market,
                ai=self.gemini_research,
                venues=tuple(self.perp_collector.providers),
                assets=self.perp_collector.assets,
                auto_ai=settings.rival_gemini_auto_enabled,
            )
            if self.forward_audit is not None and self.perp_collector is not None
            else None
        )

        self._register_commands()

    @staticmethod
    def _csv_tuple(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    def _register_commands(self) -> None:
        @self.tree.command(
            name="rival",
            description="Your best current sportsbook price opportunity, or PASS.",
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

            await self._refresh_sports_for_pick()
            await interaction.edit_original_response(
                embed=await asyncio.to_thread(
                    sportsbook_desk_embed, self.parlay_builder,
                    user_id=str(interaction.user.id),
                ),
                view=LocationView(
                    service=self.location_service,
                    parlay_builder=self.parlay_builder,
                    current_collector=self.current_collector,
                ),
            )

        @self.tree.command(
            name="rival-picks",
            description="Best current sportsbook price opportunity, or a clear PASS.",
        )
        async def rival_picks(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._refresh_sports_for_pick()
            saved = self.location_service.get_saved(
                platform="discord", user_id=str(interaction.user.id),
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
            else:
                await interaction.edit_original_response(
                    embed=await asyncio.to_thread(
                        sportsbook_desk_embed, self.parlay_builder,
                        user_id=str(interaction.user.id),
                    ),
                    view=LocationView(
                        service=self.location_service,
                        parlay_builder=self.parlay_builder,
                        current_collector=self.current_collector,
                    ),
                )

        @self.tree.command(
            name="rival-opportunities",
            description="Show current sports prices and live crypto watches, not history lessons.",
        )
        async def rival_opportunities(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._refresh_sports_for_pick()
            crypto_market = self.perp_collector.market if self.perp_collector else None
            crypto = await asyncio.to_thread(crypto_desk_embed, crypto_market)
            saved = self.location_service.get_saved(
                platform="discord", user_id=str(interaction.user.id),
            )
            if saved is None:
                sports = onboarding_embed()
            else:
                sports = await asyncio.to_thread(
                    sportsbook_desk_embed, self.parlay_builder,
                    user_id=str(interaction.user.id),
                )
            await interaction.edit_original_response(
                embeds=[sports, crypto],
                view=ResearchDashboard(
                    owner_id=interaction.user.id, market=crypto_market,
                    ai=self.gemini_research,
                    venues=tuple(self.perp_collector.providers) if self.perp_collector else (),
                    shadow_enabled=self.shadow_monitor is not None,
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
            description="Live crypto opportunities first, with deeper math and accuracy on tap.",
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

    async def _refresh_sports_for_pick(self) -> None:
        if self.current_collector is None:
            return
        try:
            await self.current_collector.refresh_if_due()
        except Exception:  # noqa: BLE001 - stale bets are rejected by builder
            _LOG.exception("Current sportsbook refresh unavailable; fail closed if stale")

    async def setup_hook(self) -> None:
        if self.current_collector is not None and self._sports_task is None:
            self._sports_task = asyncio.create_task(
                self._sports_loop(), name="rival-sports-current-refresh",
            )
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

        if (
            (self.history_auditor is not None
             or self.history_recovery is not None
             or self.kucoin_history_recovery is not None
             or self.bitget_history_recovery is not None
             or self.okx_history_recovery is not None)
            and self._history_task is None
        ):
            self._history_task = asyncio.create_task(
                self._history_loop(), name="rival-perp-history-recovery"
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

    async def _sports_loop(self) -> None:
        """Keep bet candidates fresh in the background when a paid API exists.

        Provider quota protections remain inside CurrentBoardCollector. The
        loop never places orders or auto-sends unsolicited Discord messages.
        """
        await self.wait_until_ready()
        assert self.current_collector is not None
        while not self.is_closed():
            try:
                await asyncio.wait_for(
                    self.current_collector.refresh_if_due(), timeout=45,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - provider outage cannot kill bot
                _LOG.exception("RivaL live sportsbook refresh failed safely")
            await asyncio.sleep(self.settings.rival_current_refresh_seconds)

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
            # Forward evaluation is on the same UTC 5-minute cadence as
            # market observation. It makes no exchange or Gemini API calls.
            # Failure is isolated from both collection and Discord.
            if self.autonomous is not None:
                try:
                    issued, settled, ai_issued = await self.autonomous.cycle()
                    _LOG.info(
                        "RivaL automatic research math_issued=%s "
                        "math_settled=%s ai_issued=%s",
                        issued, settled, ai_issued,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL autonomous research cycle failed safely")
            await asyncio.sleep(
                next_collection_delay(
                    datetime.now(UTC),
                    self.settings.rival_perp_collection_seconds,
                )
            )

    async def _history_loop(self) -> None:
        """Low-priority history repair; live feeds and predictions never wait."""
        await self.wait_until_ready()
        await asyncio.sleep(20)
        last_audited_hour: int | None = None
        while not self.is_closed():
            if self.history_recovery is not None:
                try:
                    inserted = await self.history_recovery.run_once()
                    if inserted:
                        _LOG.info("RivaL verified historical Gate recovery inserted=%d", inserted)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL historical recovery failed safely")
            if self.kucoin_history_recovery is not None:
                try:
                    added = await self.kucoin_history_recovery.run_once()
                    if added:
                        _LOG.info(
                            "RivaL verified historical KuCoin recovery inserted=%d",
                            added,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL KuCoin history recovery failed safely")
            if self.bitget_history_recovery is not None:
                try:
                    added = await self.bitget_history_recovery.run_once()
                    if added:
                        _LOG.info(
                            "RivaL verified historical Bitget recovery inserted=%d",
                            added,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL Bitget history recovery failed safely")
            if self.okx_history_recovery is not None:
                try:
                    added = await self.okx_history_recovery.run_once()
                    if added:
                        _LOG.info(
                            "RivaL verified historical OKX recovery inserted=%d",
                            added,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL OKX history recovery failed safely")
            if self.history_auditor is not None and self.perp_collector is not None:
                now = datetime.now(UTC)
                hour = int(now.timestamp() // 3600)
                if hour != last_audited_hour:
                    last_audited_hour = hour
                    asset = self.perp_collector.assets[hour % len(
                        self.perp_collector.assets
                    )]
                    try:
                        report = await asyncio.to_thread(
                            self.history_auditor.audit_asset,
                            asset=asset,
                            venues=tuple(self.perp_collector.providers),
                        )
                        _LOG.info(
                            "RivaL six-month history audit asset=%s streams=%d "
                            "complete=%d",
                            asset, len(report),
                            sum(x["status"] == "complete" for x in report),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        _LOG.exception("RivaL six-month history audit failed safely")
            await asyncio.sleep(self.settings.rival_history_recovery_seconds)

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
                if self.archive_runtime.perpetual_service is not None:
                    copied = await self.archive_runtime.perpetual_service.archive_ready(
                        now=datetime.now(UTC),
                        keep_hot_days=self.settings.rival_perp_archive_keep_hot_days,
                        max_rows=self.settings.rival_perp_archive_batch_rows,
                        max_batches=self.settings.rival_perp_archive_max_batches,
                    )
                    if copied:
                        _LOG.info(
                            "RivaL perpetual warehouse copied %d verified batch(es), "
                            "hot rows retained",
                            len(copied),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background archive must not kill Discord
                _LOG.exception("RivaL cold archive cycle failed safely")

            await asyncio.sleep(self.settings.rival_archive_run_seconds)

    async def close(self) -> None:
        if self._sports_task is not None:
            self._sports_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sports_task
            self._sports_task = None
        if self._history_task is not None:
            self._history_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._history_task
            self._history_task = None
        if self.history_recovery is not None:
            await self.history_recovery.aclose()
        if self.kucoin_history_recovery is not None:
            await self.kucoin_history_recovery.aclose()
        if self.bitget_history_recovery is not None:
            await self.bitget_history_recovery.aclose()
        if self.okx_history_recovery is not None:
            await self.okx_history_recovery.aclose()
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
