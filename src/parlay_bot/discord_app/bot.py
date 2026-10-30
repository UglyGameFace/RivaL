from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from parlay_bot.archive.runtime import ColdArchiveRuntime, build_cold_archive_runtime
from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.collectors.oddspapi_current import OddsPapiCurrentCollector
from parlay_bot.collectors.the_odds_api import TheOddsApiCollector
from parlay_bot.config import Settings
from parlay_bot.discord_app.data_dashboard import MarketDataDashboard
from parlay_bot.discord_app.home import GuidedHome
from parlay_bot.discord_app.multi_proof import multi_proof_embed
from parlay_bot.discord_app.opportunities import (
    crypto_desk_embed,
    sportsbook_desk_embed,
)
from parlay_bot.discord_app.presentation import onboarding_embed
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.discord_app.sports_discovery import sports_discovery_embed
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
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.gemini_research import GeminiResearch
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.live_quotes import (
    PublicBestQuoteClient,
    QuoteUnavailable,
    quote_embed,
)
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
from parlay_bot.perpetuals.paper_ledger import PaperLedger
from parlay_bot.perpetuals.price_hit_evidence import evaluate_price_hit_model
from parlay_bot.perpetuals.profit_proof import ProspectiveQuoteProfitMonitor
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.perpetuals.stream_quotes import StreamingBookHub
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger
from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.oddspapi import OddsPapiClient
from parlay_bot.providers.owner_limits import OwnerLimitStore
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
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
        if settings.sportsgameodds_api_key is not None:
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
        if settings.oddspapi_api_key is not None:
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
        if settings.the_odds_api_key is not None:
            self.the_odds_api_collector = TheOddsApiCollector(
                api_key=settings.the_odds_api_key.get_secret_value(), store=self.store,
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

        self.public_quotes = PublicBestQuoteClient()
        self.live_stream: StreamingBookHub | None = (
            StreamingBookHub(assets=tuple(
                asset for asset in self.perp_collector.assets
                if asset in ("BTC", "ETH", "SOL")
            ))
            if self.perp_collector is not None and settings.rival_stream_enabled
            else None
        )
        self.public_quotes.stream_hub = self.live_stream
        self.stream_shadow: StreamShadowLedger | None = (
            StreamShadowLedger(self.store) if self.live_stream is not None else None
        )
        self._stream_shadow_task: asyncio.Task[None] | None = None
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

        self._register_commands()

    @staticmethod
    def _csv_tuple(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

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

            home = GuidedHome(
                owner_id=interaction.user.id,
                location=self.location_service,
                builder=self.parlay_builder,
                collector=self.current_collector,
                market=collector.market if collector is not None else None,
                targets=self.direction_targets,
                paper=self.paper_ledger,
                quotes=self.public_quotes,
                profit=self.quote_profit,
                research_factory=research_factory,
                stream_shadow=self.stream_shadow,
                multi=self.autonomous.multi if self.autonomous is not None else None,
                venues=tuple(collector.providers) if collector is not None else (),
            )
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
            ],
            timeframe=[
                app_commands.Choice(name="5 minutes", value=5),
                app_commands.Choice(name="15 minutes", value=15),
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
                embed = await signal_embed(
                    market, self.public_quotes,
                    asset=coin.value, horizon_minutes=timeframe.value,
                    stream_shadow=self.stream_shadow,
                )
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
            await interaction.edit_original_response(embed=embed)

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
            description="Stop automatic personal target updates.",
        )
        async def rival_stop(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                stopped = await self.direction_targets.stop(interaction.user.id)
                message = (
                    "Target stopped. No further automatic updates."
                    if stopped else "You have no active personal target."
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL personal target cancellation failed")
                message = "Target cancellation unavailable; please retry."
            await interaction.edit_original_response(content=message)

        @tools_group.command(
            name="source-health",
            description="Inspect all ten futures collectors for the selected coin.",
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
                    content="Ten-source collection is disabled, not live.",
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
                    title=f"RivaL | {coin.value} ten-source health",
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
                    text="Five-minute candles, not ten WebSocket order books. "
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
            description="Compare the ten-venue challenger to existing predictions.",
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

        @tools_group.command(
            name="research",
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
                    target_service=self.direction_targets,
            )
            await interaction.edit_original_response(
                embed=await dashboard.render(), view=dashboard,
            )

        self.tree.add_command(tools_group)

    async def _refresh_sports_for_pick(self) -> None:
        # Use cached quotes for Discord interactions; stale selections fail
        # closed in the parlay builder. Remote collection runs in background.
        return

    async def setup_hook(self) -> None:
        if self._target_task is None:
            self._target_task = asyncio.create_task(
                self._target_loop(), name="rival-personal-direction-updates",
            )
        if (self.current_collector is not None or self.oddspapi_collector is not None or self.the_odds_api_collector is not None) and self._sports_task is None:
            self._sports_task = asyncio.create_task(
                self._sports_loop(), name="rival-sports-current-refresh",
            )
        if self.sports_results is not None and self._sports_results_task is None:
            self._sports_results_task = asyncio.create_task(
                self._sports_results_loop(), name="rival-sports-final-results",
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
                _LOG.info("RivaL sports result audit deferred by quota reserve: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - quota/provider faults never block Discord
                _LOG.exception("RivaL sports result verification unavailable")
            await asyncio.sleep(21600)

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

    async def _sports_loop(self) -> None:
        """Keep bet candidates fresh in the background when a paid API exists.

        Provider quota protections remain inside CurrentBoardCollector. The
        loop never places orders or auto-sends unsolicited Discord messages.
        """
        await self.wait_until_ready()
        assert self.current_collector is not None or self.oddspapi_collector is not None
        while not self.is_closed():
            try:
                statuses = []
                if self.current_collector is not None:
                    result = await asyncio.wait_for(
                        self.current_collector.refresh_if_due(), timeout=45,
                    )
                    statuses.append(f"SportsGameOdds: {result.events_seen} events, {result.observations_seen} odds" if result.refreshed else f"SportsGameOdds: {result.notice or 'not due'}")
                if self.oddspapi_collector is not None:
                    status = await asyncio.wait_for(self.oddspapi_collector.refresh_if_due(), timeout=45)
                    statuses.append(f"OddsPapi: {status}")
                if self.the_odds_api_collector is not None:
                    status = await asyncio.wait_for(self.the_odds_api_collector.refresh_if_due(), timeout=45)
                    statuses.append(f"The Odds API: {status}")
                self.sports_refresh_status = "; ".join(statuses)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self.sports_refresh_status = "Failed: timed out after 45 seconds"
                _LOG.warning(
                    "RivaL sportsbook refresh exceeded 45s; skipped this cycle "
                    "without marking the board fresh"
                )
            except Exception as exc:  # noqa: BLE001 - provider outage cannot kill bot
                self.sports_refresh_status = f"Failed: {type(exc).__name__}"
                _LOG.exception("RivaL live sportsbook refresh failed safely")
            self.sports_refresh_checked_at = datetime.now(UTC)
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
        if self.live_stream is not None:
            await self.live_stream.aclose()
        if self._stream_shadow_task is not None:
            self._stream_shadow_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stream_shadow_task
            self._stream_shadow_task = None
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
