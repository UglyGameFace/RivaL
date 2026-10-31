from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from parlay_bot.archive.drive import DriveArchiveFolders, GoogleDriveArchiveClient
from parlay_bot.archive.runtime import ColdArchiveRuntime, build_cold_archive_runtime
from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.collectors.oddspapi_current import OddsPapiCurrentCollector
from parlay_bot.collectors.the_odds_api import TheOddsApiCollector
from parlay_bot.config import Settings
from parlay_bot.discord_app.alert_control import stop_all_notifications
from parlay_bot.discord_app.auto_hunt import AutoHuntActions, AutoHuntService, AutoHuntStore
from parlay_bot.discord_app.cdna_public import CDNAPersonalReader
from parlay_bot.discord_app.data_dashboard import MarketDataDashboard
from parlay_bot.discord_app.home import (
    GuidedHome,
    build_stale_guided_menu_views,
    install_view_dispatch_logging,
)
from parlay_bot.discord_app.kalshi_public import KalshiCryptoBrowser
from parlay_bot.discord_app.multi_proof import multi_proof_embed
from parlay_bot.discord_app.opportunities import (
    crypto_desk_embed,
    sportsbook_desk_embed,
)
from parlay_bot.discord_app.polymarket_live_board import (
    LivePolymarketBoardService,
    LivePolymarketBoardStore,
)
from parlay_bot.discord_app.presentation import onboarding_embed
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.discord_app.sports_discovery import sports_discovery_embed
from parlay_bot.discord_app.stale_dashboards import build_stale_dashboard_views
from parlay_bot.discord_app.stream_proof import stream_proof_embed
from parlay_bot.discord_app.targeting import DirectionTargetService, DirectionTargetStore
from parlay_bot.discord_app.views import LocationView, OnboardingView
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.zip_lookup import ZipStateResolver
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.bitget_history_recovery import BitgetHistoryRecovery
from parlay_bot.perpetuals.collector import PerpetualMarketCollector
from parlay_bot.perpetuals.deribit_history_recovery import DeribitHistoryRecovery
from parlay_bot.perpetuals.dydx_history_recovery import DydxHistoryRecovery
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.gemini_research import GeminiResearch
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.hyperliquid_history_recovery import (
    HyperliquidHistoryRecovery,
)
from parlay_bot.perpetuals.kalshi_forward import KalshiForwardResearch
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.lighter_history_recovery import LighterHistoryRecovery
from parlay_bot.perpetuals.live_quotes import (
    PublicBestQuoteClient,
    QuoteUnavailable,
    quote_embed,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
from parlay_bot.perpetuals.openrouter_research import OpenRouterResearch
from parlay_bot.perpetuals.paper_ledger import PaperLedger
from parlay_bot.perpetuals.phemex_history_recovery import PhemexHistoryRecovery
from parlay_bot.perpetuals.polybolt_twap import PolyBoltTWAP
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.perpetuals.price_hit_evidence import evaluate_price_hit_model
from parlay_bot.perpetuals.profit_proof import ProspectiveQuoteProfitMonitor
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.perpetuals.stream_quotes import StreamingBookHub
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger
from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.oddspapi import OddsPapiClient
from parlay_bot.providers.owner_limits import OwnerLimitStore
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
from parlay_bot.research.database_integrity import (
    perpetual_evidence_backlog,
    sports_runtime_snapshot,
    sports_settlement_backlog,
    sports_settlement_exclusions,
)
from parlay_bot.research.dkex_reports import DKeXPublicReports
from parlay_bot.research.sports_results import (
    ResultIntegrityError,
    SportsResultStore,
)
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
        self.health_monitor = RuntimeHealthMonitor(self.store)
        self._health_task: asyncio.Task[None] | None = None
        self._health_restarts: dict[str, list[datetime]] = {}
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

        self.sports_refresh_status = 'Not attempted in this process'
        self.sports_refresh_checked_at: datetime | None = None
        self.current_provider: SportsGameOddsClient | None = None
        self.current_collector: CurrentBoardCollector | None = None
        if settings.rival_sports_background_enabled and settings.sportsgameodds_api_key is not None:
            self.current_provider = SportsGameOddsClient(
                settings.sportsgameodds_api_key.get_secret_value(),
                base_url=settings.sportsgameodds_base_url,
                monthly_entity_reserve=settings.sportsgameodds_monthly_entity_reserve,
                owner_limits=OwnerLimitStore(self.store),
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

        self.oddspapi_provider: OddsPapiClient | None = None
        self.oddspapi_collector: OddsPapiCurrentCollector | None = None
        if settings.rival_sports_background_enabled and settings.oddspapi_api_key is not None:
            self.oddspapi_provider = OddsPapiClient(
                settings.oddspapi_api_key.get_secret_value(),
                base_url=settings.oddspapi_base_url,
                quota_reserve=settings.oddspapi_quota_reserve,
                account_cache_seconds=settings.oddspapi_account_cache_seconds,
                owner_limits=OwnerLimitStore(self.store),
            )
            self.oddspapi_collector = OddsPapiCurrentCollector(
                client=self.oddspapi_provider, store=self.store,
                refresh_interval=timedelta(seconds=settings.rival_current_refresh_seconds),
            )

        self.the_odds_api_collector: TheOddsApiCollector | None = None
        if settings.rival_sports_background_enabled and settings.the_odds_api_key is not None:
            self.the_odds_api_collector = TheOddsApiCollector(
                api_key=settings.the_odds_api_key.get_secret_value(), store=self.store,
            )

        if not settings.rival_sports_background_enabled:
            _LOG.info(
                "RivaL sports background DISABLED; crypto market collection "
                "and predictions continue independently"
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
        self.sports_results = (
            SportsResultStore(self.store) if self.current_provider is not None
            else None
        )
        if self.current_collector is not None:
            self.current_collector.results = self.sports_results
        self._sports_results_task: asyncio.Task[None] | None = None
        self.command_synced_at: datetime | None = None
        self.command_sync_count = 0
        self.discord_ready_at: datetime | None = None
        self.archive_runtime: ColdArchiveRuntime | None = build_cold_archive_runtime(
            archive_settings,
            store=self.store,
        )
        self._archive_task: asyncio.Task[None] | None = None
        # Keep the offline candidate reader separate from the working
        # hourly archive uploader. A blocked models folder MUST NOT stop
        # normal PostgreSQL->Drive cold archive writes.
        self.research_drive: GoogleDriveArchiveClient | None = None
        if (
            self.archive_runtime is not None
            and settings.rival_drive_models_folder_id
        ):
            self.research_drive = GoogleDriveArchiveClient(
                client_id=str(settings.rival_drive_oauth_client_id),
                client_secret=settings.rival_drive_oauth_client_secret.get_secret_value(),
                refresh_token=settings.rival_drive_oauth_refresh_token.get_secret_value(),
                folders=DriveArchiveFolders(
                    root=str(settings.rival_drive_root_folder_id),
                    history=str(settings.rival_drive_history_folder_id),
                    manifests=str(settings.rival_drive_manifests_folder_id),
                    staging=str(settings.rival_drive_staging_folder_id),
                    models=str(settings.rival_drive_models_folder_id),
                    reports=None,
                ),
                chunk_size_bytes=settings.rival_archive_chunk_mib * 1024 * 1024,
            )
        self.offline_registry: ResearchCandidateRegistry | None = (
            ResearchCandidateRegistry(self.store)
            if self.research_drive is not None else None
        )
        self.candidate_sync_checked_at: datetime | None = None
        self.candidate_sync_status = (
            "pending_first_verified_drive_read"
            if self.offline_registry is not None
            else "not_configured_or_no_models_folder"
        )
        self._candidate_task: asyncio.Task[None] | None = None
        # Resume older verified-model pages across bounded archive cycles.
        # Starting every scan at newest used to starve all but 120 models.
        self._candidate_inventory_cursor: str | None = None

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
        self.hyperliquid_history_recovery: HyperliquidHistoryRecovery | None = (
            HyperliquidHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.deribit_history_recovery: DeribitHistoryRecovery | None = (
            DeribitHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.lighter_history_recovery: LighterHistoryRecovery | None = (
            LighterHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.dydx_history_recovery: DydxHistoryRecovery | None = (
            DydxHistoryRecovery(
                self.perp_collector.market, assets=self.perp_collector.assets,
            )
            if self.perp_collector is not None
            and settings.rival_history_recovery_enabled else None
        )
        self.phemex_history_recovery: PhemexHistoryRecovery | None = (
            PhemexHistoryRecovery(
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
        # Both OPEN_ROUTER_API_KEY (the Discloud owner's variable) and
        # OPENROUTER_API_KEY (provider convention) are accepted. OpenRouter
        # has priority when configured, but there is never a paid fallback.
        free_secret = settings.open_router_api_key or settings.openrouter_api_key
        google_secret = settings.gemini_api_key or settings.google_api_key
        if settings.rival_ai_provider == "disabled":
            self.gemini_research: GeminiResearch | OpenRouterResearch | None = None
        elif settings.rival_ai_provider == "openrouter":
            # No Gemini/paid fallback when a free coding model or the key
            # is unavailable. Only explicit legacy Gemini mode can use it.
            self.gemini_research = OpenRouterResearch(
                self.perp_collector.market if self.perp_collector is not None
                else PerpetualMarketStore(self.store),
                api_key=(free_secret.get_secret_value()
                         if free_secret is not None else None),
                daily_max=settings.rival_openrouter_daily_max,
                cooldown_seconds=settings.rival_openrouter_cooldown_seconds,
            )
            _LOG.info("RivaL optional AI reviewer: free-only OpenRouter coding")
        elif self.perp_collector is None:
            self.gemini_research = None
        else:
            self.gemini_research = GeminiResearch(
                self.perp_collector.market,
                api_key=(google_secret.get_secret_value()
                         if google_secret is not None else None),
                model=settings.rival_gemini_model,
                daily_max=settings.rival_gemini_daily_max,
                cooldown_seconds=settings.rival_gemini_cooldown_seconds,
            )
        self.autonomous: AutonomousResearch | None = (
            AutonomousResearch(
                self.perp_collector.market,
                ai=self.gemini_research,
                venues=tuple(self.perp_collector.providers),
                assets=self.perp_collector.assets,
                auto_ai=(
                    settings.rival_gemini_auto_enabled
                    and settings.rival_ai_background_opt_in
                ),
            )
            if self.forward_audit is not None and self.perp_collector is not None
            else None
        )

        self.public_quotes = PublicBestQuoteClient()
        self.live_stream: StreamingBookHub | None = (
            StreamingBookHub(assets=tuple(
                asset for asset in self.perp_collector.assets
                if asset in ("BTC", "ETH", "SOL", "XRP")
            ))
            if self.perp_collector is not None and settings.rival_stream_enabled
            else None
        )
        self.public_quotes.stream_hub = self.live_stream
        self.stream_shadow: StreamShadowLedger | None = (
            StreamShadowLedger(self.store) if self.live_stream is not None else None
        )
        self._stream_shadow_task: asyncio.Task[None] | None = None
        # Redundant momentum-notification delivery was retired; streaming
        # books still feed forward research and quote provenance.
        self.quote_profit = (
            ProspectiveQuoteProfitMonitor(self.store)
            if self.forward_audit is not None else None
        )
        self.paper_ledger = PaperLedger(self.store)
        self.direction_targets = DirectionTargetService(
            bot=self,
            market=self.perp_collector.market if self.perp_collector else None,
            store=DirectionTargetStore(self.store),
        )
        self._target_task: asyncio.Task[None] | None = None
        self.auto_hunt = AutoHuntService(
            bot=self,
            market=self.perp_collector.market if self.perp_collector else None,
            quotes=self.public_quotes, store=AutoHuntStore(self.store),
            stream_shadow=self.stream_shadow,
            # Retired duplicated alert DMs must never promise delivery.
            # Single-card Polymarket and forecast tracking remain available.
            alerts_enabled=False,
        )
        self.polymarket_research = PolymarketResearch(
            self.store, ai=self.gemini_research
            if isinstance(self.gemini_research, OpenRouterResearch) else None,
        )
        self.auto_hunt.contract_research = self.polymarket_research
        def secret_value(value):
            return value.get_secret_value() if value is not None else None
        self.polybolt = PolyBoltTWAP(
            api_key=secret_value(settings.polymarket_clob_api_key),
            secret=secret_value(settings.polymarket_clob_api_secret),
            passphrase=secret_value(settings.polymarket_clob_api_passphrase),
        )
        self._polybolt_task: asyncio.Task[None] | None = None
        self.kalshi_forward = KalshiForwardResearch(self.store)
        self.kalshi_browser = KalshiCryptoBrowser(forward=self.kalshi_forward)
        # Owner-triggered personal-only reference; never feed to AI,
        # learning, persistence, or automatic prediction collectors.
        self.cdna_personal = CDNAPersonalReader()
        self._kalshi_research_task: asyncio.Task[None] | None = None
        self.polymarket_board = LivePolymarketBoardService(
            bot=self,
            store=LivePolymarketBoardStore(self.store),
            reader=self.auto_hunt.polymarket,
            market=self.perp_collector.market if self.perp_collector else None,
            research=self.polymarket_research,
        )
        self._poly_board_task: asyncio.Task[None] | None = None
        self._poly_research_task: asyncio.Task[None] | None = None

        self._register_commands()

    @staticmethod
    def _csv_tuple(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    def _build_guided_home(self, owner_id: int) -> GuidedHome:
        """One fresh private menu for a user; shared by /rival and the
        stale-menu fallback so a recovered menu is built identically."""
        collector = self.perp_collector

        def research_factory(owner_id: int) -> ResearchDashboard:
            return ResearchDashboard(
                owner_id=owner_id,
                market=collector.market if collector is not None else None,
                ai=self.gemini_research,
                venues=tuple(collector.providers) if collector is not None else (),
                shadow_enabled=self.shadow_monitor is not None,
                target_service=self.direction_targets,
            )

        return GuidedHome(
            owner_id=owner_id,
            location=self.location_service,
            builder=self.parlay_builder,
            collector=self.current_collector,
            refresh_sports=self._request_sports_refresh,
            market=collector.market if collector is not None else None,
            targets=self.direction_targets,
            auto_hunt=self.auto_hunt,
            paper=self.paper_ledger,
            quotes=self.public_quotes,
            profit=self.quote_profit,
            research_factory=research_factory,
            stream_shadow=self.stream_shadow,
            multi=self.autonomous.multi if self.autonomous is not None else None,
            venues=tuple(collector.providers) if collector is not None else (),
            training_status=self.training_status,
            polymarket_status=self.polymarket_learning_status,
            dkex_status=DKeXPublicReports(self.store).summary,
            kalshi=self.kalshi_browser,
            kalshi_status=self.kalshi_forward.summary,
            cdna_personal=self.cdna_personal,
        )

    def _build_research_dashboard(self, owner_id: int) -> ResearchDashboard:
        collector = self.perp_collector
        return ResearchDashboard(
            owner_id=owner_id,
            market=collector.market if collector is not None else None,
            ai=self.gemini_research,
            venues=tuple(collector.providers) if collector is not None else (),
            shadow_enabled=self.shadow_monitor is not None,
            target_service=self.direction_targets,
        )

    def _build_data_dashboard(self, owner_id: int) -> MarketDataDashboard:
        collector = self.perp_collector
        return MarketDataDashboard(
            owner_id=owner_id,
            market=collector.market if collector is not None else None,
            venues=tuple(collector.providers) if collector is not None else (),
            assets=collector.assets if collector is not None else (),
            interval_seconds=self.settings.rival_perp_collection_seconds,
            enabled=collector is not None,
        )

    def _register_commands(self) -> None:
        # Three obvious shortcuts plus one optional tools namespace.
        # All legacy operations remain available; ordinary users navigate
        # /rival instead of being shown twenty competing global commands.
        tools_group = app_commands.Group(
            name="rival-tools",
            description="Optional advanced features. Use /rival for the easy menu.",
        )
        @self.tree.command(
            name="rival",
            description="Open RivaL's simple private home: sports, crypto, practice and help.",
        )
        async def rival(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            home = self._build_guided_home(interaction.user.id)
            await interaction.edit_original_response(
                embed=await home.render(), view=home,
            )

        @self.tree.command(
            name="rival-signal",
            description="Show a fresh crypto UP/DOWN watch or explain why no signal qualifies.",
        )
        @app_commands.choices(
            coin=[
                app_commands.Choice(name="Bitcoin (BTC)", value="BTC"),
                app_commands.Choice(name="Ethereum (ETH)", value="ETH"),
                app_commands.Choice(name="Solana (SOL)", value="SOL"),
                app_commands.Choice(name="XRP", value="XRP"),
            ],
            timeframe=[
                app_commands.Choice(name="5 minutes", value=5),
                app_commands.Choice(name="15 minutes", value=15),
                app_commands.Choice(name="60 minutes", value=60),
            ],
        )
        async def rival_signal(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
            timeframe: app_commands.Choice[int],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                market = self.perp_collector.market if self.perp_collector else None
                embed = await asyncio.wait_for(signal_embed(
                    market, self.public_quotes,
                    asset=coin.value, horizon_minutes=timeframe.value,
                    stream_shadow=self.stream_shadow,
                ), timeout=10)
            except Exception:  # noqa: BLE001 - never reveal backend details
                _LOG.exception("RivaL verified signal rendering failed")
                embed = discord.Embed(
                    title="⏳ Signal temporarily unavailable",
                    description=(
                        "The current market and model evidence could not be "
                        "verified. No trade instruction was issued."
                    ),
                    color=discord.Color.orange(),
                )
            try:
                await interaction.edit_original_response(embed=embed)
            except (discord.NotFound, discord.HTTPException):
                _LOG.warning("RivaL signal response expired or unavailable")

        @tools_group.command(
            name="picks",
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

        @tools_group.command(
            name="opportunities",
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
                    target_service=self.direction_targets,
                ),
            )

        @tools_group.command(
            name="leagues",
            description="See sports and leagues actually authorized by your odds provider.",
        )
        async def rival_leagues(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.current_provider is None:
                await interaction.edit_original_response(
                    content="⛔ SportsGameOdds API key is not configured. No authorized catalog."
                )
                return
            try:
                leagues = await self.current_provider.get_available_leagues()
                if not leagues:
                    await interaction.edit_original_response(
                        content="No eligible sports leagues confirmed by your provider plan."
                    )
                    return
                sports: dict[str, list[str]] = {}
                for league in leagues:
                    sports.setdefault(league["sport_id"], []).append(league["name"])
                embed = discord.Embed(
                    title="RivaL • Provider-Authorized Leagues",
                    description=(
                        f"**{len(leagues)} leagues / {len(sports)} sports** "
                        "returned by your API key. Access is not the same as "
                        "live odds, availability or a validated predictive model."
                    ),
                    color=discord.Color.blurple(),
                )
                for sport, names in sorted(sports.items())[:15]:
                    embed.add_field(
                        name=sport[:80],
                        value=", ".join(names)[:240], inline=False,
                    )
                if len(sports) > 15:
                    embed.set_footer(text="Additional sports omitted from compact phone view")
                await interaction.edit_original_response(embed=embed)
            except Exception:  # noqa: BLE001 - hide provider response and API keys
                _LOG.exception("RivaL provider league catalog unavailable")
                await interaction.edit_original_response(
                    content="⛔ Could not verify entitled leagues. No invented coverage."
                )

        @tools_group.command(
            name="sport-proof",
            description="Show independently scored soccer or basketball model evidence.",
        )
        @app_commands.describe(
            sport="Soccer Poisson or basketball scoring model",
            league="Exact official provider league ID, such as EPL or NBA",
        )
        @app_commands.choices(sport=[
            app_commands.Choice(name="Soccer", value="SOCCER"),
            app_commands.Choice(name="Basketball", value="BASKETBALL"),
        ])
        async def rival_sport_proof(
            interaction: discord.Interaction,
            sport: app_commands.Choice[str],
            league: str,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.sports_results is None:
                await interaction.edit_original_response(
                    content="⛔ No authorized sports results provider configured."
                )
                return
            if not 1 <= len(league.strip()) <= 64:
                await interaction.edit_original_response(
                    content="⛔ Enter the provider's exact league ID."
                )
                return
            try:
                result = await asyncio.to_thread(
                    self.sports_results.evaluate,
                    sport=sport.value, league=league.strip().upper(),
                )
                embed = discord.Embed(
                    title=f"RivaL • {sport.value.title()} • {result.league}",
                    description=(
                        f"**{result.status}**\n{result.reason}\n"
                        "Historical model qualification is not a proven "
                        "real-money edge or an approved wager."
                    ),
                    color=discord.Color.orange(),
                )
                review = await asyncio.to_thread(
                    self.sports_results.promotion_status,
                    sport=sport.value, league=league.strip().upper(),
                )
                embed.add_field(
                    name="Verified finalized games",
                    value=str(result.train_events),
                )
                embed.add_field(
                    name="Prospective model governance",
                    value=(
                        f"{review['status']} • "
                        f"{review['settled_unique_events']} settled pregame forecasts, "
                        f"{review['utc_days']} UTC days • "
                        "No real-money approval"
                    ), inline=False,
                )
                embed.add_field(
                    name="Chronological model evaluation",
                    value=(
                        f"{result.evaluated} held-out games / "
                        f"{result.evaluated_days} dates\n"
                        f"Brier: {result.model_brier if result.model_brier is not None else 'n/a'}"
                        f" vs baseline "
                        f"{result.baseline_brier if result.baseline_brier is not None else 'n/a'}"
                    ), inline=False,
                )
                embed.set_footer(
                    text="Source: verified provider-final scores • Not a profitable entry signal"
                )
                await interaction.edit_original_response(embed=embed)
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL sports model proof unavailable")
                await interaction.edit_original_response(
                    content="⛔ Cannot verify model evidence. NO BET."
                )

        @tools_group.command(
            name="health",
            description="Check Discord command sync, workers and verified data state.",
        )
        async def rival_health(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            online = self.is_ready() and self.discord_ready_at is not None
            workers = (
                ("Sports odds", self._sports_task),
                ("Sport results", self._sports_results_task),
                ("Perpetual data", self._perp_task),
                ("Model evaluation", self._shadow_task),
                ("Polymarket oracle", self._polybolt_task),
                ("Kalshi learning", self._kalshi_research_task),
                ("Personal alerts", self._target_task),
            )
            embed = discord.Embed(
                title="RivaL • Live Runtime Diagnostics",
                description=(
                    "**This response proves Discord is accepting interactions.** "
                    "Workers marked running are not proof that their outside "
                    "data providers are delivering valid prices/results."
                ),
                color=discord.Color.green() if online else discord.Color.orange(),
            )
            embed.add_field(
                name="Discord / command sync",
                value=(
                    f"{'READY' if online else 'NOT READY'} • "
                    f"{self.command_sync_count} command(s) synced"
                    + (f" {discord.utils.format_dt(self.command_synced_at, style='R')}"
                       if self.command_synced_at else " • sync not confirmed")
                ), inline=False,
            )
            embed.add_field(
                name="Background workers",
                value="\n".join(
                    f"{name}: "
                    f"{'disabled' if task is None else 'STOPPED' if task.done() else 'running'}"
                    for name, task in workers
                ), inline=False,
            )
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.to_thread(self.health_monitor.snapshot), timeout=4,
                )
                problems = snapshot["issues"]
                detail = (
                    f"Last host watchdog: {snapshot['checked_at'] or 'never'}\n"
                    + ("Open incidents: " + ", ".join(
                        p["issue_key"] for p in problems[:8])
                       if problems else "No currently persisted incidents")
                )
            except Exception:  # noqa: BLE001
                detail = "Host health database unverified"
            embed.add_field(
                name="Independent host watchdog",
                value=detail[:1024], inline=False,
            )
            try:
                latest = await asyncio.to_thread(
                    self.store.get_runtime_state,
                    "sportsgameodds:last_successful_refresh",
                )
                stamp = datetime.fromisoformat(latest) if latest else None
                description = (
                    "Last successful sportsbook refresh: "
                    + discord.utils.format_dt(stamp, style="R")
                    if stamp is not None and stamp.utcoffset() is not None
                    else "No verified sportsbook refresh recorded"
                )
            except Exception:  # noqa: BLE001
                description = "Sportsbook refresh state unavailable"
            embed.add_field(name="Actual source status", value=description, inline=False)
            try:
                recovery = await asyncio.wait_for(
                    asyncio.to_thread(sports_runtime_snapshot, self.store),
                    timeout=4,
                )
                last = recovery["last_cycle"]
                last_text = (
                    f"{last['at']} UTC • provider={last.get('provider', 'unknown')}"
                    f" • new finals={last.get('new_finals', 0)}"
                    f" • local reconciled={last.get('reconciled', 0)}"
                    if last is not None else
                    "No completed poll/reconciliation cycle recorded"
                )
                exclusions = recovery["exclusions"]
                exclusion_text = ", ".join(
                    f"{reason}: {count}" for reason, count in sorted(exclusions.items())
                ) or "none"
                recovery_text = (
                    f"Saved provider-final results: {recovery['final_events']}"
                    f" • latest {recovery['last_final_at'] or 'none'}\n"
                    f"Scored prospective forecasts: {recovery['scored_forecasts']}"
                    f" • latest {recovery['last_scored_at'] or 'none'}\n"
                    f"Saved finals awaiting score: {recovery['unsettled_finalized']}\n"
                    f"Started fixtures lacking final evidence: "
                    f"{recovery['started_missing_results']}\n"
                    f"Excluded forecasts (not wins): {exclusion_text[:170]}\n"
                    f"Last worker cycle: {last_text}"
                )
            except Exception:  # noqa: BLE001 - preserve command ACK if DB is slow
                _LOG.exception("RivaL hosted sports settlement snapshot unavailable")
                recovery_text = (
                    "Live database settlement proof unavailable. "
                    "A successful Discloud upload is not settlement evidence."
                )
            embed.add_field(
                name="Hosted sports settlement recovery",
                value=recovery_text[:1024], inline=False,
            )
            oracle_status = self.polybolt.status()
            oracle_text = (
                "PolyBolt TWAP: "
                + (
                    "LIVE verified oracle samples" if oracle_status["valid_twap_live"]
                    else "authenticated, awaiting fresh TWAP"
                    if oracle_status["connected"]
                    else "authenticated, awaiting subscription"
                    if oracle_status["authenticated"]
                    else "credentials needed" if not oracle_status["configured"]
                    else "not authenticated"
                )
                + "\nFresh Chainlink assets: "
                + (", ".join(oracle_status["assets_fresh"])
                   if oracle_status["assets_fresh"] else "none verified")
                + "\nSource samples accepted since start: "
                + str(oracle_status["accepted_twap_samples"])
                + "\nLast valid source update: "
                + (oracle_status["last_valid_twap_at"] or "none")
                + "\nConnections attempted: "
                + str(oracle_status["reconnects"])
                + "\nProvider state: "
                + str(oracle_status["last_error"] or "no error reported")
                + "\nOracle-v2 remains research-only"
            )
            embed.add_field(
                name="Polymarket oracle research",
                value=oracle_text, inline=False,
            )
            embed.add_field(name="Latest refresh attempt", value=self.sports_refresh_status, inline=False)
            embed.set_footer(
                text="Use /rival-quote and /rival-sport-proof for separate market/model evidence"
            )
            await interaction.edit_original_response(embed=embed)

        @tools_group.command(
            name="sports",
            description="Discover sports and leagues actually present in fresh odds feeds.",
        )
        async def rival_sports(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._refresh_sports_for_pick()
            await interaction.edit_original_response(
                embed=await asyncio.to_thread(
                    sports_discovery_embed, self.store,
                    configured_leagues=(
                        self.current_collector.league_ids
                        if self.current_collector is not None else ()
                    ),
                    collector_enabled=self.current_collector is not None,
                ),
            )

        @tools_group.command(
            name="quote",
            description="Current verified perpetual bid/ask, spread and assumed cost (no trade).",
        )
        @app_commands.describe(
            coin="BTC, ETH or SOL futures on Binance USD-M",
            quantity="Optional asset quantity for checking top-of-book liquidity",
        )
        @app_commands.choices(coin=[
            app_commands.Choice(name="Bitcoin (BTC)", value="BTC"),
            app_commands.Choice(name="Ethereum (ETH)", value="ETH"),
            app_commands.Choice(name="Solana (SOL)", value="SOL"),
        ])
        async def rival_quote(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
            quantity: float = 0.001,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                quote = await self.public_quotes.get(coin.value)
                embed = quote_embed(quote, quantity=quantity)
                await interaction.edit_original_response(embed=embed)
            except (QuoteUnavailable, ValueError) as exc:
                await interaction.edit_original_response(
                    content=f"⛔ Live quote unavailable: {exc} No substitute candle price."
                )
            except Exception:  # noqa: BLE001 - no provider/auth internals in Discord
                _LOG.exception("RivaL public best quote unavailable")
                await interaction.edit_original_response(
                    content="⛔ Could not verify a live bid/ask. PASS.",
                )

        @tools_group.command(
            name="profit-proof",
            description="See actual prospective bid/ask paper profit evidence and missing data.",
        )
        @app_commands.describe(
            coin="BTC, ETH or SOL futures",
            timeframe="Independent 5 or 15-minute forecast horizon",
        )
        @app_commands.choices(
            coin=[
                app_commands.Choice(name="BTC", value="BTC"),
                app_commands.Choice(name="ETH", value="ETH"),
                app_commands.Choice(name="SOL", value="SOL"),
            ],
            timeframe=[
                app_commands.Choice(name="5 minutes", value=5),
                app_commands.Choice(name="15 minutes", value=15),
            ],
        )
        async def rival_profit_proof(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
            timeframe: app_commands.Choice[int],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.stream_shadow is not None:
                # The old 5-minute Binance-only ledger is retained as history,
                # but live claims now use ONLY the strict source-matched audit.
                try:
                    result = await asyncio.to_thread(
                        self.stream_shadow.report,
                        asset=coin.value, horizon_minutes=timeframe.value,
                    )
                    await interaction.edit_original_response(
                        embed=stream_proof_embed(result),
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL strict profit evidence unavailable")
                    await interaction.edit_original_response(
                        content="Strict after-cost evidence unavailable. NOT PROVEN.",
                    )
                return
            if self.quote_profit is None:
                await interaction.edit_original_response(
                    content="⛔ No prospective quote ledger is enabled. Profit NOT PROVEN."
                )
                return
            try:
                proof = await asyncio.to_thread(
                    self.quote_profit.proof,
                    asset=coin.value, horizon_minutes=timeframe.value,
                )
                embed = discord.Embed(
                    title=f"RivaL • {coin.value} {timeframe.value}m Profit Verification",
                    description=(
                        "**REAL PROFIT: NOT VERIFIED**\\n"
                        + proof.explanation
                        + "\\nNo exchange orders were placed by this test."
                    ),
                    color=discord.Color.orange(),
                )
                embed.add_field(
                    name="Prospective observation coverage",
                    value=(
                        f"{proof.verified_quote_exits} completed entry/exit quotes "
                        f"of {proof.eligible_forecast_windows} potential "
                        f"forecast windows ({proof.coverage:.1%}); "
                        f"{proof.missing_exits} missed exits."
                    ), inline=False,
                )
                embed.add_field(
                    name="After-cost paper mean",
                    value=(
                        f"Conservative: "
                        f"{proof.net_conservative_bps:+.2f} bps"
                        if proof.net_conservative_bps is not None
                        else "No completed quote-paired paper trades"
                    ) + (
                        f"\\nSevere stress: {proof.net_severe_bps:+.2f} bps"
                        if proof.net_severe_bps is not None else ""
                    ), inline=False,
                )
                embed.add_field(
                    name="Time-ordered confidence screen",
                    value=(
                        f"{proof.utc_days} UTC days; "
                        f"conservative daily lower: "
                        f"{proof.conservative_daily_lower_bps:+.2f} bps"
                        if proof.conservative_daily_lower_bps is not None
                        else f"{proof.utc_days} UTC days; no valid daily lower bound"
                    ) + (
                        f"\\nSevere daily lower: {proof.severe_daily_lower_bps:+.2f} bps"
                        if proof.severe_daily_lower_bps is not None else ""
                    ), inline=False,
                )
                embed.add_field(
                    name="Shadow qualification",
                    value=(
                        "PASS on observed quote-based assumptions; "
                        "real-money proof still absent"
                        if proof.quote_shadow_qualified else "NOT QUALIFIED"
                    ), inline=False,
                )
                embed.set_footer(
                    text=(
                        "Fresh Binance USD-M bid/ask observations • "
                        "hypothetical 25 USDT trade • assumed fees/funding/slippage "
                        "• never verified fills"
                    )
                )
                await interaction.edit_original_response(embed=embed)
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL prospective profit verification unavailable")
                await interaction.edit_original_response(
                    content="⛔ Cannot verify forward trading returns. NOT PROVEN."
                )

        @tools_group.command(
            name="paper-open",
            description="Open a limited, quote-based PAPER position (never a real order).",
        )
        @app_commands.describe(
            coin="Paper BTC/ETH/SOL perpetual",
            direction="Long expects price to rise, short expects price to fall",
            notional_usdt="Paper notional in USDT, maximum 250",
        )
        @app_commands.choices(
            coin=[
                app_commands.Choice(name="BTC", value="BTC"),
                app_commands.Choice(name="ETH", value="ETH"),
                app_commands.Choice(name="SOL", value="SOL"),
            ],
            direction=[
                app_commands.Choice(name="Long (paper)", value="long"),
                app_commands.Choice(name="Short (paper)", value="short"),
            ],
        )
        async def rival_paper_open(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
            direction: app_commands.Choice[str],
            notional_usdt: float = 25.0,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                quote = await self.public_quotes.get(coin.value)
                message = await asyncio.to_thread(
                    self.paper_ledger.open,
                    user_id=str(interaction.user.id),
                    position_id=str(interaction.id),
                    quote=quote, side=direction.value,
                    notional_usdt=notional_usdt,
                )
                await interaction.edit_original_response(content="🧪 " + message)
            except (QuoteUnavailable, ValueError) as exc:
                await interaction.edit_original_response(
                    content=f"⛔ Paper position not opened: {exc}",
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL paper open unavailable")
                await interaction.edit_original_response(
                    content="⛔ Could not verify a paper entry. No real order.",
                )

        @tools_group.command(
            name="paper-close",
            description="Close your quote-based PAPER position (not a real exchange trade).",
        )
        @app_commands.choices(coin=[
            app_commands.Choice(name="BTC", value="BTC"),
            app_commands.Choice(name="ETH", value="ETH"),
            app_commands.Choice(name="SOL", value="SOL"),
        ])
        async def rival_paper_close(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                quote = await self.public_quotes.get(coin.value)
                closed = await asyncio.to_thread(
                    self.paper_ledger.close,
                    user_id=str(interaction.user.id), asset=coin.value,
                    quote=quote,
                )
                await interaction.edit_original_response(content=(
                    f"🧪 Paper {closed.side.upper()} {coin.value} closed. "
                    f"Indicative entry ${closed.entry:,.4f}, exit "
                    f"${closed.exit:,.4f}. Gross ${closed.gross_usdt:,.4f}; "
                    f"assumed costs ${closed.assumed_cost_usdt:,.4f}; "
                    f"**paper-only net ${closed.assumed_net_usdt:,.4f}**. "
                    "No authenticated fills; not actual P&L."
                ))
            except (QuoteUnavailable, ValueError) as exc:
                await interaction.edit_original_response(
                    content=f"⛔ Paper close unavailable: {exc}",
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL paper close unavailable")
                await interaction.edit_original_response(
                    content="⛔ Paper close unavailable; position unchanged.",
                )

        @tools_group.command(
            name="practice",
            description="Fund your fictional USDT bankroll and view practice risk and progress.",
        )
        @app_commands.describe(
            funded_usdt=(
                "Optional starting amount (100–100000); omit to view your existing funds"
            ),
        )
        async def rival_practice(
            interaction: discord.Interaction,
            funded_usdt: float | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await asyncio.to_thread(
                    self.paper_ledger.fund, str(interaction.user.id),
                    starting_usdt=funded_usdt,
                )
                cash = result["practice"]
                embed = discord.Embed(
                    title="🧪 RivaL • Practice Funded Account",
                    description=(
                        "**100% fictional funds.** No deposit, payout, "
                        "real order, actual trading return, or leverage."
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(
                    name="Starting practice balance",
                    value=f"${cash['funded']:,.2f} USDT",
                )
                embed.add_field(
                    name="Practice equity",
                    value=f"${cash['equity']:,.2f} USDT",
                )
                embed.add_field(
                    name="Available / reserved",
                    value=(
                        f"${cash['available']:,.2f} / "
                        f"${cash['committed']:,.2f} USDT"
                    ), inline=False,
                )
                embed.add_field(
                    name="Simulated outcomes",
                    value=(
                        f"{cash['completed']} complete • "
                        f"{cash['wins']} wins • {cash['losses']} losses\n"
                        f"Estimated realized net: ${cash['realized']:+,.2f} USDT"
                    ), inline=False,
                )
                embed.add_field(
                    name="Daily practice loss budget",
                    value=(
                        f"${cash['day_loss']:,.2f} used / "
                        f"${cash['daily_loss_cap']:,.2f} limit"
                    ), inline=False,
                )
                cohorts = result["practice_feedback"]["validated_cohorts"]
                if cohorts:
                    embed.add_field(
                        name="Your quote-based practice patterns",
                        value="\\n".join(
                            f"{x['asset']} {x['side']}: "
                            f"{x['wins']}/{x['settled']} profitable, "
                            f"mean net ${x['mean_net_usdt']:+,.2f} USDT"
                            for x in cohorts[:6]
                        ), inline=False,
                    )
                embed.set_footer(
                    text=(
                        "Trade feedback improves research visibility; "
                        "only independent forward outcomes qualify AI updates."
                    ),
                )
                await interaction.edit_original_response(embed=embed)
            except ValueError as exc:
                await interaction.edit_original_response(
                    content=f"⛔ Practice funding refused: {exc}"
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL private practice account unavailable")
                await interaction.edit_original_response(
                    content="⛔ Practice account unavailable. No balance was credited."
                )

        @tools_group.command(
            name="paper-results",
            description="Your isolated paper ledger, including losses and fees.",
        )
        async def rival_paper_results(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await asyncio.to_thread(
                    self.paper_ledger.summary, str(interaction.user.id),
                )
                await interaction.edit_original_response(
                    content=(
                        f"🧪 **Paper only:** {result['open']} open, "
                        f"{result['closed']} closed. "
                        f"After assumed fees and slippage: "
                        f"**${result['assumed_net_usdt']:,.4f} USDT**.\\n"
                        f"Fictional equity: **${result['practice']['equity']:,.2f}** "
                        f"• Free: **${result['practice']['available']:,.2f}** "
                        f"• Reserved: ${result['practice']['committed']:,.2f}.\\n"
                        "Real-world P&L unverified; simulated decisions "
                        "cannot authorize model promotion."
                    ),
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL paper summary unavailable")
                await interaction.edit_original_response(
                    content="⛔ Paper ledger unavailable; no P&L asserted.",
                )

        @tools_group.command(
            name="target-proof",
            description="Independent forward evidence for your saved exact-price target.",
        )
        async def rival_target_proof(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                target = await asyncio.to_thread(
                    self.direction_targets.store.get, str(interaction.user.id),
                )
                if target is None or target.price_goal is None:
                    await interaction.edit_original_response(
                        content="No saved exact-price target. Use /rival-target with target_price."
                    )
                    return
                market = self.perp_collector.market if self.perp_collector else None
                result = await asyncio.to_thread(
                    evaluate_price_hit_model, market,
                    goal=target.price_goal, horizon_minutes=target.horizon_minutes,
                )
                embed = discord.Embed(
                    title=f"RivaL • {target.asset} {target.horizon_minutes}m target proof",
                    description=result.reason,
                    color=discord.Color.blurple() if result.qualified
                          else discord.Color.orange(),
                )
                embed.add_field(
                    name="Price-touch model",
                    value=(f"Research P(HIT): **{result.probability:.1%}**"
                           if result.qualified else
                           "**NOT QUALIFIED** • No trustworthy target-hit probability"),
                    inline=False,
                )
                embed.add_field(
                    name="Independent chronological evaluation",
                    value=(
                        f"Train {result.trained} • Holdout {result.holdout} "
                        f"across {result.holdout_days} UTC days\\n"
                        f"Brier {result.brier if result.brier is not None else 'n/a'} "
                        f"vs baseline "
                        f"{result.baseline_brier if result.baseline_brier is not None else 'n/a'}"
                    ), inline=False,
                )
                embed.set_footer(
                    text="Shadow empirical research, not a cost-positive bet or model promotion."
                )
                await interaction.edit_original_response(embed=embed)
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL target research proof query unavailable")
                await interaction.edit_original_response(
                    content="⛔ Target probability research unavailable; no estimate published."
                )

        @tools_group.command(
            name="target",
            description="Watch UP/DOWN or an exact crypto USDT price, automatically.",
        )
        @app_commands.describe(
            coin="Which crypto perpetual market should RivaL monitor?",
            timeframe="Watch a 5m or 15m window.",
            target_price="Optional: exact USDT price to watch for a verified hit.",
        )
        @app_commands.choices(
            coin=[
                app_commands.Choice(name="Bitcoin (BTC)", value="BTC"),
                app_commands.Choice(name="Ethereum (ETH)", value="ETH"),
                app_commands.Choice(name="Solana (SOL)", value="SOL"),
            ],
            timeframe=[
                app_commands.Choice(name="5 minutes", value=5),
                app_commands.Choice(name="15 minutes", value=15),
            ],
        )
        async def rival_target(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
            timeframe: app_commands.Choice[int],
            target_price: float | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                ok, message = await self.direction_targets.follow(
                    interaction.user,
                    asset=coin.value, horizon_minutes=timeframe.value,
                    target_price=target_price,
                )
            except ValueError as exc:
                ok, message = False, str(exc)
            except Exception:  # noqa: BLE001 - never expose Discord or DB secrets
                _LOG.exception("RivaL personal target registration failed")
                ok, message = False, "Target setup unavailable; no target was changed."
            await interaction.edit_original_response(
                content=("✅ " if ok else "⛔ ") + message,
            )

        @self.tree.command(
            name="rival-stop",
            description="Stop all your personal RivaL notifications and tracking.",
        )
        async def rival_stop(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            message = await stop_all_notifications(
                interaction.user.id,
                auto_hunt=self.auto_hunt,
                targets=self.direction_targets,
                tracker=self.polymarket_board,
            )
            await interaction.edit_original_response(content=message)

        @tools_group.command(
            name="source-health",
            description="Inspect all nine futures collectors for the selected coin.",
        )
        @app_commands.choices(coin=[
            app_commands.Choice(name="BTC", value="BTC"),
            app_commands.Choice(name="ETH", value="ETH"),
            app_commands.Choice(name="SOL", value="SOL"),
        ])
        async def rival_source_health(
            interaction: discord.Interaction, coin: app_commands.Choice[str],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.perp_collector is None:
                await interaction.edit_original_response(
                    content="Nine-source collection is disabled, not live.",
                )
                return
            try:
                rows = await asyncio.to_thread(self.perp_collector.market.feed_health)
                health = {(r["venue"],r["asset"]):r for r in rows}
                stamp = datetime.now(UTC)
                description = []
                fresh = 0
                for venue in self.perp_collector.providers:
                    row = health.get((venue,coin.value))
                    if row is None:
                        description.append(f"⏳ **{venue}** • Never observed")
                        continue
                    seen = row.get("last_success_at")
                    candle = row.get("last_candle_close")
                    ok = False
                    if seen and candle:
                        try:
                            age = stamp - datetime.fromisoformat(str(candle))
                            attempted = stamp - datetime.fromisoformat(str(seen))
                            ok = (
                                row["status"] == "ok"
                                and timedelta(0) <= age <= timedelta(minutes=15)
                                and timedelta(0) <= attempted <= timedelta(minutes=15)
                            )
                        except (ValueError, TypeError):
                            pass
                    fresh += int(ok)
                    status = "Fresh candles" if ok else (
                        f"Not fresh • {row['status']}"
                    )
                    description.append(
                        f"{'✅' if ok else '⏳'} **{venue}** • {status}"
                        f" • failures {row['failure_count']}"
                    )
                embed = discord.Embed(
                    title=f"RivaL | {coin.value} nine-source health",
                    description=(
                        f"**{fresh}/{len(self.perp_collector.providers)}** "
                        "venues confirmed recently fresh from actual stored "
                        "candle and collection timestamps.\n"
                        + "\n".join(description)
                    ),
                    color=discord.Color.blurple() if fresh >= 3
                          else discord.Color.orange(),
                )
                embed.set_footer(
                    text="Five-minute candles, not live order books. "
                         "A failed or region-blocked source never counts as fresh.",
                )
                await interaction.edit_original_response(embed=embed)
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL all-venue source health unavailable")
                await interaction.edit_original_response(
                    content="Source freshness couldn't be verified. NOT FRESH.",
                )

        @tools_group.command(
            name="multi-proof",
            description="Compare the nine-venue challenger to existing predictions.",
        )
        @app_commands.choices(
            coin=[
                app_commands.Choice(name="BTC", value="BTC"),
                app_commands.Choice(name="ETH", value="ETH"),
                app_commands.Choice(name="SOL", value="SOL"),
            ],
            timeframe=[
                app_commands.Choice(name="5 minutes", value=5),
                app_commands.Choice(name="15 minutes", value=15),
            ],
        )
        async def rival_multi_proof(
            interaction: discord.Interaction,
            coin: app_commands.Choice[str],
            timeframe: app_commands.Choice[int],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.autonomous is None:
                await interaction.edit_original_response(
                    content="Multi-source shadow model is disabled, not approved.",
                )
                return
            try:
                value = await asyncio.to_thread(
                    self.autonomous.multi.report,
                    asset=coin.value, horizon_minutes=timeframe.value,
                )
                await interaction.edit_original_response(
                    embed=multi_proof_embed(value),
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("Multi-exchange evidence unavailable")
                await interaction.edit_original_response(
                    content="No verified multi-source outcome evidence available.",
                )

        @tools_group.command(
            name="stream-status",
            description="Check live BTC/ETH/SOL exchange streams and freshness.",
        )
        async def rival_stream_status(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.live_stream is None:
                await interaction.edit_original_response(
                    content="Streaming disabled or no PostgreSQL perpetual collector. "
                            "No real-time signal service is claimed.",
                )
                return
            status = self.live_stream.health()
            embed = discord.Embed(
                title="📡 RivaL | Live stream health",
                description=(
                    "Source-specific live best books. Fresh means the last "
                    "verified exchange event was within 10 seconds, not that "
                    "a model has proved profitable."
                ),
                color=discord.Color.blurple(),
            )
            for venue, info in status.items():
                embed.add_field(
                    name=venue.title(),
                    value=(
                        f"Connected: **{'yes' if info['connected'] else 'no'}** • "
                        f"Fresh: **{', '.join(info['fresh_assets']) or 'none'}**\\n"
                        f"Verified updates: {info['updates']} • "
                        f"Reconnects: {info['reconnects']}"
                    ),
                    inline=False,
                )
            await interaction.edit_original_response(embed=embed)

        @tools_group.command(
            name="stream-proof",
            description="Check source-matched, forward-only after-cost shadow results.",
        )
        @app_commands.choices(
            coin=[
                app_commands.Choice(name="BTC", value="BTC"),
                app_commands.Choice(name="ETH", value="ETH"),
                app_commands.Choice(name="SOL", value="SOL"),
            ],
            timeframe=[
                app_commands.Choice(name="5 minutes", value=5),
                app_commands.Choice(name="15 minutes", value=15),
            ],
        )
        async def rival_stream_proof(
            interaction: discord.Interaction, coin: app_commands.Choice[str],
            timeframe: app_commands.Choice[int],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.stream_shadow is None:
                await interaction.edit_original_response(
                    content="Live shadow scoring disabled. No returns verified.",
                )
                return
            try:
                report = await asyncio.to_thread(
                    self.stream_shadow.report,
                    asset=coin.value, horizon_minutes=timeframe.value,
                )
                embed = stream_proof_embed(report)
                await interaction.edit_original_response(embed=embed)
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL stream proof unavailable")
                await interaction.edit_original_response(
                    content="Live quote shadow evidence unavailable, no profit claimed.",
                )

        @tools_group.command(
            name="data",
            description="Open the private RivaL market data dashboard.",
        )
        async def rival_data(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            dashboard = self._build_data_dashboard(interaction.user.id)
            await interaction.edit_original_response(
                embed=await dashboard.render(), view=dashboard,
            )

        @tools_group.command(
            name="research",
            description="Live crypto opportunities first, with deeper math and accuracy on tap.",
        )
        async def rival_research(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            dashboard = self._build_research_dashboard(interaction.user.id)
            await interaction.edit_original_response(
                embed=await dashboard.render(), view=dashboard,
            )

        self.tree.add_command(tools_group)

    async def _refresh_sports_for_pick(self) -> None:
        # Use cached quotes for Discord interactions; stale selections fail
        # closed in the parlay builder. Remote collection runs in background.
        return

    async def setup_hook(self) -> None:
        install_view_dispatch_logging(self)
        # Persistent fallback for guided menus that outlived their live view
        # (15-minute timeout or bot restart). Without this, taps on those
        # menus are silently discarded and the menu looks dead.
        for stale_view in build_stale_guided_menu_views(self._build_guided_home):
            self.add_view(stale_view)
        for stale_view in build_stale_dashboard_views(
            self._build_research_dashboard, self._build_data_dashboard,
        ):
            self.add_view(stale_view)
        self.add_view(AutoHuntActions())
        _LOG.info("RivaL guided, research, data and hunt recovery views registered")
        await asyncio.to_thread(self.auto_hunt.store.initialize)
        await asyncio.to_thread(self.polymarket_board.store.initialize)
        await asyncio.to_thread(self.polymarket_research.initialize)
        await asyncio.to_thread(self.kalshi_forward.initialize)
        _LOG.info(
            "RivaL Polymarket contract AI wired free_key_configured=%s; "
            "exact 5m/15m/1h contracts, experimental calls only",
            self.polymarket_research.ai is not None and self.polymarket_research.ai.available,
        )
        # Install read/forecast schemas before starting any collection,
        # notification or learning workers. Creating indexes while another
        # worker inserts predictions caused live PostgreSQL deadlocks.
        if self.autonomous is not None:
            await asyncio.to_thread(self.autonomous.initialize)
        # Retired duplicated Auto-Hunt alert producers. Live Polymarket
        # tracking and optional verified direction-shift notices continue.
        if self._target_task is None:
            self._target_task = asyncio.create_task(
                self._target_loop(), name="rival-personal-direction-updates",
            )
        if self._poly_board_task is None:
            self._poly_board_task = asyncio.create_task(
                self._poly_board_loop(), name="rival-auto-polymarket-board",
            )
        if (
            self.settings.rival_polybolt_enabled
            and self.polybolt.configured
            and self._polybolt_task is None
        ):
            self._polybolt_task = asyncio.create_task(
                self.polybolt.run(), name="rival-read-only-chainlink-twap",
            )
            _LOG.info(
                "RivaL PolyBolt read-only worker started; "
                "CLOB credentials configured; awaiting authenticated price proof"
            )
        elif not self.polybolt.configured:
            _LOG.info(
                "RivaL oracle-v2 waiting for official PolyBolt CLOB API credentials; "
                "book-only Polymarket research remains available"
            )
        if self._poly_research_task is None:
            self._poly_research_task = asyncio.create_task(
                self._poly_research_loop(), name="rival-silent-polymarket-learning",
            )
        if self._kalshi_research_task is None:
            self._kalshi_research_task = asyncio.create_task(
                self._kalshi_research_loop(), name="rival-silent-kalshi-research",
            )
        if (self.current_collector is not None or self.oddspapi_collector is not None or self.the_odds_api_collector is not None) and self._sports_task is None:
            self._sports_task = asyncio.create_task(
                self._sports_loop(), name="rival-sports-current-refresh",
            )
        if self.sports_results is not None and self._sports_results_task is None:
            self._sports_results_task = asyncio.create_task(
                self._sports_results_loop(), name="rival-sports-final-results",
            )
        # A sync failure must not abort the workers below: data collection
        # and DM follows keep working, and the missing commands are visible
        # as sync_count=0 / sync not confirmed instead of a half-started bot.
        try:
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
        except (discord.HTTPException, TimeoutError):
            _LOG.warning("RivaL command sync failed; workers still starting")
            synced = []
        else:
            self.command_synced_at = datetime.now(UTC)
        self.command_sync_count = len(synced)
        _LOG.info(
            "RivaL actual Discord slash command sync verified count=%d",
            self.command_sync_count,
        )

        if self.live_stream is not None:
            self.live_stream.start()
            if self._stream_shadow_task is None:
                self._stream_shadow_task = asyncio.create_task(
                    self._stream_shadow_loop(), name="rival-stream-forward-cost-audit",
                )
            _LOG.info(
                "RivaL websocket market feeds started assets=%s venues=hyperliquid,binance",
                ",".join(self.live_stream.assets),
            )
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
             or self.okx_history_recovery is not None
             or self.hyperliquid_history_recovery is not None
             or self.deribit_history_recovery is not None
             or self.lighter_history_recovery is not None
             or self.dydx_history_recovery is not None
             or self.phemex_history_recovery is not None)
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
        if self.offline_registry is not None and self._candidate_task is None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.offline_registry.initialize),
                    timeout=3,
                )
            except Exception:  # noqa: BLE001 - archive proof is optional
                self.candidate_sync_status = "candidate_registry_unavailable"
                _LOG.exception(
                    "RivaL optional historical registry could not initialize; "
                    "Discord and primary archives continue",
                )
            else:
                self._candidate_task = asyncio.create_task(
                    self._candidate_import_loop(),
                    name="rival-verified-offline-candidate-import",
                )
        # Independent of Discord commands and data collection. A dead worker
        # cannot rely on its own finally block to report a failure.
        try:
            await asyncio.to_thread(self.health_monitor.initialize)
            self._health_task = asyncio.create_task(
                self._health_loop(), name="rival-process-health-watchdog",
            )
        except Exception:  # noqa: BLE001 - monitoring must not stop trading-free research
            _LOG.exception("RivaL host watchdog could not initialize")

    async def on_ready(self) -> None:
        self.discord_ready_at = datetime.now(UTC)
        _LOG.info(
            "RivaL Discord READY user_id=%s sync_count=%d",
            self.user.id if self.user else "unknown",
            self.command_sync_count,
        )

    async def _sports_results_loop(self) -> None:
        """Bounded, low-priority final-score audits for actual prior fixtures."""
        await self.wait_until_ready()
        await asyncio.sleep(45)
        assert self.sports_results is not None
        assert self.current_provider is not None
        while not self.is_closed():
            cycle_provider = "not_due"
            cycle_new_finals = 0
            cycle_reconciled = 0
            try:
                now = datetime.now(UTC)
                due = await asyncio.to_thread(
                    self.sports_results.due_fixtures, now=now, limit=8,
                )
                if due:
                    events = await asyncio.wait_for(
                        self.current_provider.get_result_events(tuple(due)), timeout=40,
                    )
                    seen = datetime.now(UTC)
                    verified = 0
                    affected: set[tuple[str, str]] = set()
                    for event in events:
                        if event.get("status", {}).get("finalized") is not True:
                            continue
                        try:
                            result = await asyncio.to_thread(
                                self.sports_results.save_finalized,
                                event, observed_at=seen,
                            )
                            verified += int(result == "inserted")
                            if result == "inserted":
                                affected.add((
                                    str(event["sportID"]), str(event["leagueID"]),
                                ))
                        except ResultIntegrityError:
                            _LOG.warning(
                                "RivaL finalized sports result withheld: conflicting "
                                "or incomplete provider evidence"
                            )
                    cycle_provider = "checked"
                    cycle_new_finals = verified
                    await asyncio.to_thread(
                        self.sports_results.mark_checked, due, now=seen,
                    )
                    for sport, league in affected:
                        review = await asyncio.to_thread(
                            self.sports_results.promotion_status,
                            sport=sport, league=league, now=seen,
                        )
                        _LOG.info(
                            "RivaL automatic research model governance "
                            "sport=%s league=%s status=%s settled=%d",
                            sport, league, review["status"],
                            review["settled_unique_events"],
                        )
                    _LOG.info(
                        "RivaL finalized score audit checked=%d newly_verified=%d",
                        len(due), verified,
                    )
            except QuotaReserveReached as exc:
                cycle_provider = "quota_deferred"
                _LOG.info("RivaL sports result audit deferred by quota reserve: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - quota/provider faults never block Discord
                cycle_provider = "unavailable"
                _LOG.exception("RivaL sports result verification unavailable")
            # This is local PostgreSQL work, not another provider request.
            # Run it even when no fixtures were due, the provider is down,
            # or the quota reserve prevented fetching any results.
            try:
                reconciled = await asyncio.to_thread(
                    self.sports_results.reconcile_unsettled, limit=100,
                )
                cycle_reconciled = len(reconciled)
                if reconciled:
                    _LOG.info(
                        "RivaL sports backlog repaired events=%d leagues=%d",
                        len(reconciled), len(set(reconciled)),
                    )
                for sport, league in set(reconciled):
                    review = await asyncio.to_thread(
                        self.sports_results.promotion_status,
                        sport=sport, league=league, now=datetime.now(UTC),
                    )
                    _LOG.info(
                        "RivaL recovered sports governance sport=%s league=%s "
                        "status=%s settled=%d",
                        sport, league, review["status"],
                        review["settled_unique_events"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - repair must not block Discord
                _LOG.exception("RivaL sports settlement reconciliation failed")
            try:
                remaining = await asyncio.to_thread(
                    sports_settlement_backlog, self.store,
                )
                excluded = await asyncio.to_thread(sports_settlement_exclusions, self.store)
                _LOG.info(
                    "RivaL sports DB integrity unsettled_finalized=%d exclusions=%s",
                    remaining, excluded,
                )
                await asyncio.to_thread(
                    self.store.set_runtime_state,
                    "rival:sports-results:last-cycle",
                    json.dumps({
                        "at": datetime.now(UTC).isoformat(),
                        "provider": cycle_provider,
                        "new_finals": cycle_new_finals,
                        "reconciled": cycle_reconciled,
                        "unsettled_finalized": remaining,
                        "exclusions": excluded,
                    }, sort_keys=True, separators=(",", ":")),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - monitoring is best effort
                _LOG.exception("RivaL sports DB integrity check unavailable")
            # Every 30 minutes recheck saved results; provider-specific
            # 6-hour per-fixture retry and shared quota reserves remain intact.
            await asyncio.sleep(1800)

    async def _kalshi_research_loop(self) -> None:
        """Quietly collect real public Kalshi quotes and settled receipts.

        Rotate assets to cap REST load. Provider errors never interrupt
        Discord interaction handling and never create a notification.
        """
        await self.wait_until_ready()
        assets = ("BTC", "ETH", "SOL", "XRP")
        index = 0
        while not self.is_closed():
            started = time.monotonic()
            asset = assets[index % len(assets)]
            index += 1
            try:
                # _study is separate and asynchronous so it can't hold up UI.
                snapshot = await asyncio.wait_for(
                    self.kalshi_browser.browse(asset, refresh=True),
                    timeout=20,
                )
                _LOG.info(
                    "RivaL silent Kalshi research asset=%s series=%d "
                    "markets=%d notifications=0 ai_requests=0",
                    asset, snapshot.matched_series, len(snapshot.markets),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "RivaL silent Kalshi data unavailable asset=%s", asset,
                    exc_info=True,
                )
            await asyncio.sleep(max(1, 120 - (time.monotonic() - started)))

    async def _poly_research_loop(self) -> None:
        """Study genuine listed contracts silently, without consuming AI quota.

        The original market snapshot is frozen per exact contract. Only a
        verified binary terminal resolution can become a new training label.
        Never send DMs for price ticks or for incomplete model evidence.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            started = time.monotonic()
            try:
                # One shared capped public read, not one network request per
                # Discord subscriber. Live tracker reuses the same reader.
                notices = await asyncio.wait_for(
                    self.auto_hunt.polymarket.active(), timeout=16,
                )
                budget = asyncio.Semaphore(2)

                async def study(notice, limiter=budget) -> bool:
                    async with limiter:
                        try:
                            from dataclasses import replace
                            # Only an authenticated, nonstale and same-source
                            # PolyBolt reference may feed oracle v2 training.
                            reference = (
                                self.polybolt.reference(notice)
                                if self.settings.rival_polybolt_enabled else None
                            )
                            enriched = (
                                replace(notice, oracle_reference=reference)
                                if reference is not None else notice
                            )
                            await asyncio.wait_for(
                                asyncio.to_thread(
                                    self.polymarket_research.study,
                                    enriched,
                                ), timeout=8,
                            )
                            return True
                        except Exception:  # noqa: BLE001 - isolate source
                            _LOG.exception(
                                "RivaL silent Polymarket study failed asset=%s horizon=%s",
                                notice.asset, notice.horizon,
                            )
                            return False

                studied = await asyncio.gather(*(study(n) for n in notices))
                if studied:
                    _LOG.info(
                        "RivaL silent Polymarket research observed=%d studied=%d "
                        "notifications=0 ai_requests=0",
                        len(notices), sum(studied),
                    )
                self.polymarket_research.maybe_settle(self.auto_hunt.polymarket)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL silent Polymarket learning temporarily unavailable")
            await asyncio.sleep(max(1, 90 - (time.monotonic() - started)))

    async def _poly_board_loop(self) -> None:
        """Automatically roll verified markets and forecasts in existing DMs."""
        await self.wait_until_ready()
        while not self.is_closed():
            started = time.monotonic()
            try:
                changed = await self.polymarket_board.cycle()
                # Receipt settlement is still required for learning even
                # though duplicated Polymarket Auto-Hunt notices are retired.
                # This schedules an asyncio task, and must run on the event
                # loop, not inside a worker thread.
                self.polymarket_research.maybe_settle(self.auto_hunt.polymarket)
                if changed:
                    _LOG.info(
                        "RivaL verified live Polymarket boards updated=%d",
                        changed,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL Polymarket live board cycle failed safely")
            # Distinct from the 1s RAM-only live direction scanner. CLOB
            # requests are rate-bounded and grouped by asset/window.
            await asyncio.sleep(max(0.1, 15 - (time.monotonic() - started)))

    async def _target_loop(self) -> None:
        """Update user-opted-in private forecast cards without flooding DMs."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                edited = await self.direction_targets.cycle()
                if edited:
                    _LOG.info("RivaL target cards updated=%d", edited)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - Discord uptime independent
                _LOG.exception("RivaL target refresh cycle failed safely")
            await asyncio.sleep(60)

    def _request_sports_refresh(self) -> str:
        if getattr(self, "_sports_manual_task", None) is not None and not self._sports_manual_task.done():
            return "Refresh already running. No new odds confirmed."
        self._sports_manual_task = asyncio.create_task(
            self._refresh_all_sports(), name="rival-manual-sports-refresh",
        )
        return "Checking all three odds providers in background. No new odds confirmed yet."

    async def _refresh_all_sports(self) -> str:
        """Refresh providers independently so one quota/error cannot starve others."""
        sources = (
            ("SportsGameOdds", self.current_collector),
            ("OddsPapi", self.oddspapi_collector),
            ("The Odds API", self.the_odds_api_collector),
        )
        async def check(name, collector):
            if collector is None:
                return f"{name}: disabled"
            try:
                result = await asyncio.wait_for(collector.refresh_if_due(), timeout=45)
                if isinstance(result, str):
                    return f"{name}: {result}"
                if result.refreshed:
                    return f"{name}: {result.events_seen} events, {result.observations_seen} odds"
                return f"{name}: {result.notice or 'not due'}"
            except TimeoutError:
                return f"{name}: timed out (no fresh odds confirmed)"
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("RivaL %s refresh failed: %s", name, type(exc).__name__)
                return f"{name}: unavailable ({type(exc).__name__})"

        statuses = await asyncio.gather(*(check(name, source) for name, source in sources))
        self.sports_refresh_status = "; ".join(statuses)
        self.sports_refresh_checked_at = datetime.now(UTC)
        return self.sports_refresh_status

    async def _sports_loop(self) -> None:
        """Refresh each sportsbook source without blocking Discord interactions."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._refresh_all_sports()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL sportsbook scheduler unavailable")
            await asyncio.sleep(self.settings.rival_current_refresh_seconds)

    async def _stream_shadow_loop(self) -> None:
        """Sample fresh WS books for read-only forward cost proof, never raw ticks.

        REST is deliberately not polled here. Missing data stays missing.
        The old five-minute provenance ledger remains intact for comparison.
        """
        await self.wait_until_ready()
        assert self.stream_shadow is not None
        assert self.live_stream is not None
        _LOG.info("RivaL streaming quote forward shadow RUNNING")
        while not self.is_closed():
            try:
                await asyncio.to_thread(self.stream_shadow.expire)
                if self.autonomous is not None:
                    await asyncio.to_thread(self.autonomous.multi.expire)
                for venue in ("hyperliquid", "binance"):
                    for asset in self.live_stream.assets:
                        book = self.live_stream.snapshot(venue, asset)
                        if book is None:
                            continue
                        try:
                            result = await asyncio.to_thread(
                                self.stream_shadow.observe, book,
                            )
                            if self.autonomous is not None:
                                multi_result = await asyncio.to_thread(
                                    self.autonomous.multi.observe, book,
                                )
                                if multi_result["opened"] or multi_result["closed"]:
                                    _LOG.info(
                                        "RivaL multi-source paired-quote "
                                        "venue=%s asset=%s opened=%d closed=%d",
                                        venue, asset, multi_result["opened"],
                                        multi_result["closed"],
                                    )
                            if result["opened"] or result["closed"] or result["expired"]:
                                _LOG.info(
                                    "RivaL forward quote venue=%s asset=%s "
                                    "entered=%d exited=%d missed=%d",
                                    venue, asset, result["opened"],
                                    result["closed"], result["expired"],
                                )
                        except Exception:  # noqa: BLE001 - isolate per market
                            _LOG.exception(
                                "RivaL streaming forward proof failed venue=%s asset=%s",
                                venue, asset,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never bring Discord down
                _LOG.exception("RivaL streaming forward scoring temporarily unavailable")
            await asyncio.sleep(self.settings.rival_stream_shadow_seconds)

    async def _perp_loop(self) -> None:
        await self.wait_until_ready()
        assert self.perp_collector is not None
        _LOG.info("RivaL perp automatic collector RUNNING")
        last_integrity_hour: int | None = None
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
            # Once per hour, inspect persisted evidence without adding API
            # requests or disturbing live predictions and market collection.
            if self.autonomous is not None:
                integrity_hour = int(datetime.now(UTC).timestamp() // 3600)
                if integrity_hour != last_integrity_hour:
                    try:
                        audit = await asyncio.to_thread(
                            perpetual_evidence_backlog, self.store,
                        )
                        last_integrity_hour = integrity_hour
                        _LOG.info(
                            "RivaL perpetual DB integrity overdue=%d "
                            "recoverable=%d quoted=%d neither_traded=%d",
                            audit["overdue_forecasts"],
                            audit["recoverable_overdue"],
                            audit["closed_quote_windows"],
                            audit["neither_traded_quote_windows"],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - read-only audit only
                        _LOG.exception("RivaL perpetual DB integrity unavailable")
            # Prospective execution research is strictly read-only and
            # refuses to backfill old candles as executable market quotes.
            # Three assets × one snapshot per shared collector cycle, not
            # a REST request per subscriber or Discord guild.
            if self.quote_profit is not None:
                try:
                    await asyncio.to_thread(self.quote_profit.expire_unobserved)
                    # The new streaming ledger owns prospective entries/exits.
                    # Preserve historical legacy rows, but stop duplicate
                    # 5-minute cross-venue quote accounting and REST polling.
                    if self.live_stream is None:
                        for asset in self.perp_collector.assets:
                            if asset not in ("BTC", "ETH", "SOL"):
                                continue
                            try:
                                quote = await self.public_quotes.get(asset)
                                result = await asyncio.to_thread(
                                    self.quote_profit.observe, quote,
                                )
                                if result["opened"] or result["closed"]:
                                    _LOG.info(
                                        "RivaL paper quote forward asset=%s opened=%d "
                                        "closed=%d unverified_expired=%d",
                                        asset, result["opened"], result["closed"],
                                        result["expired"],
                                    )
                            except QuoteUnavailable:
                                # Provider/region/geoblock makes observations
                                # unavailable, NEVER profitable by assumption.
                                continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL quote forward accounting unavailable")
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
        # Backfills are bulk research, not a live signal dependency. The old
        # loop ran nine providers x four assets in one startup burst, holding
        # the shared PostgreSQL pool while users were pressing Get Signal.
        # Rotate at most two providers each cycle; cursors remain durable.
        recovery_jobs = tuple(
            (name, recovery) for name, recovery in (
                ("Gate", self.history_recovery),
                ("KuCoin", self.kucoin_history_recovery),
                ("Bitget", self.bitget_history_recovery),
                ("OKX", self.okx_history_recovery),
                ("Hyperliquid", self.hyperliquid_history_recovery),
                ("Deribit", self.deribit_history_recovery),
                ("Lighter", self.lighter_history_recovery),
                ("dYdX", self.dydx_history_recovery),
                ("Phemex", self.phemex_history_recovery),
            ) if recovery is not None
        )
        recovery_cursor = 0
        while not self.is_closed():
            for _ in range(min(2, len(recovery_jobs))):
                name, recovery = recovery_jobs[recovery_cursor % len(recovery_jobs)]
                recovery_cursor += 1
                try:
                    inserted = await recovery.run_once()
                    if inserted:
                        _LOG.info(
                            "RivaL verified historical %s recovery inserted=%d",
                            name, inserted,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - independent recovery providers
                    _LOG.exception("RivaL historical %s recovery failed safely", name)
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
                            "complete=%d incomplete=[%s]",
                            asset, len(report),
                            sum(x["status"] == "complete" for x in report),
                            "; ".join(
                                f"{x['venue']}:missing={x['missing_bars']}/"
                                f"gaps={x['gap_segments']}/"
                                f"invalid={x['invalid_observations']}"
                                for x in report if x["status"] != "complete"
                            ),
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

    async def _candidate_import_loop(self) -> None:
        """Read bounded checksum-verified offline models from private Drive.

        Matching screened weights become research challengers. Trade promotion
        still needs forward and cost evidence. No credentials in logs.
        """
        await self.wait_until_ready()
        assert self.research_drive is not None
        assert self.offline_registry is not None
        while not self.is_closed():
            try:
                saved = 0
                pages = 0
                cursor = self._candidate_inventory_cursor
                seen_cursors = set()
                # A single latest-ten listing can exclude every older coin,
                # timeframe and trained model when new artifacts are published.
                # Bound the scan, keep all existing candidates immutable, and
                # perform expensive reads off the Discord interaction path.
                for _ in range(12):
                    items, next_cursor = await asyncio.wait_for(
                        self.research_drive.read_verified_research_candidates(
                            limit=20, page_token=cursor, return_next_page=True,
                        ), timeout=90,
                    )
                    pages += 1
                    for file_id, payload in items:
                        try:
                            imported = await asyncio.to_thread(
                                self.offline_registry.import_document,
                                file_id, payload,
                            )
                            saved += imported
                        except ValueError:
                            _LOG.warning(
                                "RivaL offline research candidate failed "
                                "model-contract verification"
                            )
                    if not next_cursor:
                        break
                    if next_cursor == cursor or next_cursor in seen_cursors:
                        raise ValueError("repeated verified-model Drive cursor")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                if next_cursor:
                    # Continue at the next page on the next six-hour scan;
                    # don't perpetually re-import only the newest models.
                    self._candidate_inventory_cursor = next_cursor
                    _LOG.info(
                        "RivaL verified model scan reached %d pages; "
                        "older archive pages resume on next cycle", pages,
                    )
                else:
                    self._candidate_inventory_cursor = None
                self.candidate_sync_status = f"verified_read_{saved}_candidates"
                self.candidate_sync_checked_at = datetime.now(UTC)
                _LOG.info("RivaL verified historical candidate reads=%d", saved)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # Drive pagination tokens can expire when new files arrive.
                # On a failed read, recover from newest; never mark an
                # unverified page as a successful model import.
                self._candidate_inventory_cursor = None
                self.candidate_sync_status = "drive_read_unverified"
                self.candidate_sync_checked_at = datetime.now(UTC)
                _LOG.exception("RivaL candidate import from Drive unavailable")
            await asyncio.sleep(21600)

    async def _health_loop(self) -> None:
        """Recover exited workers only, never clone a possibly busy worker."""
        await self.wait_until_ready()
        while not self.is_closed():
            faults: dict[str, str] = {}
            now = datetime.now(UTC)
            workers = (
                ("_perp_task", self._perp_loop, "rival-perp-auto-collector"),
                ("_shadow_task", self._shadow_loop, "rival-shadow-learning"),
                ("_history_task", self._history_loop, "rival-perp-history-recovery"),
                ("_candidate_task", self._candidate_import_loop,
                 "rival-verified-offline-candidate-import"),
                ("_stream_shadow_task", self._stream_shadow_loop,
                 "rival-stream-forward-cost-audit"),
                ("_archive_task", self._archive_loop, "rival-cold-archive"),
                ("_sports_results_task", self._sports_results_loop,
                 "rival-sports-final-results"),
                ("_sports_task", self._sports_loop, "rival-sports-current-refresh"),
                ("_kalshi_research_task", self._kalshi_research_loop,
                 "rival-silent-kalshi-research"),
                ("_poly_research_task", self._poly_research_loop,
                 "rival-silent-polymarket-learning"),
                ("_poly_board_task", self._poly_board_loop,
                 "rival-auto-polymarket-board"),
                ("_target_task", self._target_loop, "rival-personal-direction-updates"),
            )
            for attr, method, name in workers:
                task = getattr(self, attr)
                if task is None or not task.done():
                    continue
                faults[f"worker:{name}"] = "background worker exited unexpectedly"
                # Three restarts per rolling hour, at most one per minute.
                # Cancelling a blocked to_thread DB operation then starting
                # a duplicate worker is unsafe, so pending tasks are never
                # force-restarted. Persistently dead workers remain alerted.
                previous = [
                    t for t in self._health_restarts.get(attr, [])
                    if now-t < timedelta(hours=1)
                ]
                self._health_restarts[attr] = previous
                if len(previous) >= 3 or (
                    previous and now-previous[-1] < timedelta(minutes=1)
                ):
                    faults[f"worker:{name}"] = "restart budget exhausted or cooling down"
                    continue
                self._health_restarts[attr].append(now)
                setattr(self, attr, asyncio.create_task(method(), name=name))
                _LOG.warning("RivaL watchdog restarted exited worker %s", name)
            try:
                feeds = (
                    tuple((venue, asset)
                          for venue in self.perp_collector.providers
                          for asset in self.perp_collector.assets)
                    if self.perp_collector is not None else ()
                )
                observed = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.health_monitor.inspect, now=now,
                        expected_feeds=feeds,
                        learning_enabled=self.autonomous is not None,
                        streaming_enabled=self.live_stream is not None,
                        import_enabled=self.offline_registry is not None,
                        import_checked_at=self.candidate_sync_checked_at,
                        import_status=self.candidate_sync_status,
                    ),
                    timeout=15,
                )
                faults.update(observed)
                # The underlying normal settlement cycle already performs the
                # same idempotent recovery. Help when it is delayed, but never
                # manufacture missing target candles or overwrite outcomes.
                if ("settlement_backlog" in observed and
                        self.forward_audit is not None):
                    recovered = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.forward_audit.reconcile_stale_settlements,
                            now, limit=120,
                        ), timeout=15,
                    )
                    if recovered:
                        _LOG.info(
                            "RivaL watchdog repaired %d causally settleable forecasts",
                            recovered,
                        )
                events = await asyncio.wait_for(
                    asyncio.to_thread(self.health_monitor.publish, faults, now=now),
                    timeout=15,
                )
                webhook = os.environ.get("RIVAL_HEALTH_DISCORD_WEBHOOK_URL", "")
                if (events and webhook.startswith((
                    "https://discord.com/api/webhooks/",
                    "https://discordapp.com/api/webhooks/",
                ))):
                    # Dedicated optional owner alert route; no user DMs or
                    # market data secrets. Transition-only messages.
                    async with httpx.AsyncClient(timeout=5) as client:
                        for state, key, detail in events[:8]:
                            try:
                                await client.post(
                                    webhook,
                                    json={"content": (
                                        f"RivaL host health {state}: "
                                        f"{key} • {detail}"
                                    )[:1900]},
                                )
                            except (httpx.HTTPError, TimeoutError):
                                _LOG.exception("RivaL operator health alert delivery failed")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - independent watcher keeps polling
                _LOG.exception(
                    "RivaL watchdog check unavailable; health is unverified"
                )
            await asyncio.sleep(120)

    def polymarket_learning_status(self, asset: str, horizon: int) -> dict:
        """Read Polymarket contract scores without querying exchange/Drive state."""
        self.polymarket_research.initialize()
        return self.polymarket_research.learner.summary(asset=asset, horizon=horizon)

    def training_status(self, asset: str, horizon: int) -> dict:
        """Read-only accuracy and provenance. No retroactive promotion."""
        from parlay_bot.perpetuals.promotion import promoted_engine

        self.polymarket_research.initialize()
        offline = (
            self.offline_registry.summary(
                asset=asset, horizon=horizon,
                live_venues=tuple(self.perp_collector.providers) if self.perp_collector else (),
            )
            if self.offline_registry is not None else
            {"total": 0, "candidate": None, "active_offline_model": False}
        )
        live = (
            self.autonomous.learning_heartbeat()
            if self.autonomous is not None else None
        )
        engine = (
            promoted_engine(self.store, asset=asset, horizon=horizon)
            if self.autonomous is not None else "unavailable"
        )
        return {
            **offline, "live_engine": engine,
            "live_learning": live,
            "drive_checked_at": self.candidate_sync_checked_at,
            "drive_status": self.candidate_sync_status,
            "polymarket_learning": self.polymarket_research.learner.summary(asset=asset, horizon=horizon),
            "model_checkpoints": (
                self.perp_collector.market.live_model_checkpoints.summary()
                if self.perp_collector is not None
                and self.perp_collector.market.live_model_checkpoints is not None else None
            ),
        }

    async def _archive_loop(self) -> None:
        await self.wait_until_ready()
        assert self.archive_runtime is not None

        while not self.is_closed():
            try:
                cutoff = datetime.now(UTC) - timedelta(
                    minutes=self.settings.rival_archive_closed_state_minutes
                )
                completed = (
                    await self.archive_runtime.service.archive_ready(
                        cutoff=cutoff,
                        max_rows=self.settings.rival_archive_batch_rows,
                        max_batches=self.settings.rival_archive_max_batches,
                    )
                    if self.settings.rival_sports_archive_enabled else []
                )
                if completed:
                    _LOG.info(
                        "RivaL cold archive completed %d batch(es)",
                        len(completed),
                    )
                # Reuse the existing private archive only. A completed
                # source/month file is retrospective data, never a forecast.
                if self.perp_collector is not None:
                    from parlay_bot.research.source_native import NativeMonthlyPublisher

                    try:
                        published = await NativeMonthlyPublisher(
                            self.perp_collector.market, self.archive_runtime.drive,
                            self.settings.rival_archive_local_dir,
                        ).run_once(now=datetime.now(UTC), max_batches=2)
                        if published:
                            _LOG.info(
                                "RivaL source-exact research archived=%d complete "
                                "venue/month snapshots; live calls unchanged",
                                len(published),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - preserve the existing archive
                        _LOG.exception(
                            "RivaL optional source-native publication failed safely"
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
        if self._health_task is not None:
            self._health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        if self._polybolt_task is not None:
            self._polybolt_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._polybolt_task
            self._polybolt_task = None
        if self._kalshi_research_task is not None:
            self._kalshi_research_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._kalshi_research_task
            self._kalshi_research_task = None
        await self.kalshi_browser.aclose()
        await self.cdna_personal.aclose()
        if self._poly_board_task is not None:
            self._poly_board_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poly_board_task
            self._poly_board_task = None
        if self._poly_research_task is not None:
            self._poly_research_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poly_research_task
            self._poly_research_task = None
        if self.live_stream is not None:
            await self.live_stream.aclose()
        if self._stream_shadow_task is not None:
            self._stream_shadow_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stream_shadow_task
            self._stream_shadow_task = None
        await self.polymarket_board.aclose()
        await self.auto_hunt.aclose()
        await self.polymarket_research.aclose()
        await self.public_quotes.aclose()
        if self._target_task is not None:
            self._target_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._target_task
            self._target_task = None
        if self._sports_results_task is not None:
            self._sports_results_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sports_results_task
            self._sports_results_task = None
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

        if self._candidate_task is not None:
            self._candidate_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._candidate_task
            self._candidate_task = None
        if self._archive_task is not None:
            self._archive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._archive_task
            self._archive_task = None

        if self.research_drive is not None:
            await self.research_drive.aclose()
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
