from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # A blank variable in .env.example (notably RIVAL_DEV_GUILD_ID=)
        # must mean "unset", not "" passed to an int field: without this,
        # pydantic-settings raises int_parsing before the bot starts.
        env_ignore_empty=True,
    )

    the_odds_api_key: SecretStr | None = Field(default=None)
    oddspapi_api_key: SecretStr | None = Field(default=None)
    oddspapi_base_url: str = Field(default="https://api.oddspapi.io")
    oddspapi_quota_reserve: int = Field(default=20, ge=0)
    oddspapi_account_cache_seconds: int = Field(default=60, ge=0)

    # Sports is OPTIONAL. Key presence must never start sports polling.
    rival_sports_background_enabled: bool = Field(default=False)
    rival_sports_archive_enabled: bool = Field(default=False)
    sportsgameodds_api_key: SecretStr | None = Field(default=None)
    sportsgameodds_base_url: str = Field(default="https://api.sportsgameodds.com/v2")
    sportsgameodds_monthly_entity_reserve: int = Field(default=250, ge=0)
    rival_current_leagues: str = Field(default="NBA,NFL")
    rival_current_bookmakers: str = Field(default="draftkings,fanduel")
    rival_current_refresh_seconds: int = Field(default=600, ge=60)
    rival_current_event_limit: int = Field(default=25, ge=1, le=100)
    rival_current_max_pages: int = Field(default=1, ge=1, le=5)

    rival_storage_backend: Literal["postgres", "sqlite"] = Field(default="postgres")
    rival_database_url: SecretStr | None = Field(default=None)
    rival_db_path: str = Field(default="data/rival.sqlite")
    discord_token: SecretStr | None = Field(default=None)
    rival_dev_guild_id: int | None = Field(default=None, ge=1)

    # Automatic read-only perpetual observations start on the PostgreSQL bot.
    # A failed/blocked provider cannot stop Discord or the other venue.
    rival_perp_collection_enabled: bool = Field(default=True)
    rival_perp_collection_assets: str = Field(default="BTC,ETH,SOL,XRP")
    rival_perp_collection_seconds: int = Field(default=300, ge=300, le=3600)
    rival_perp_collection_bars: int = Field(default=48, ge=2, le=48)
    # Shared, read-only websocket feeds; offload only verified quote samples
    # into forward shadow research. Never persist a database row per tick.
    rival_stream_enabled: bool = Field(default=True)
    rival_stream_shadow_seconds: int = Field(default=30, ge=10, le=60)

    # Bounded read-only historical recovery (Gate Futures only initially).
    # New data is marked by actual first_observed_at; never synthetic live PnL.
    rival_history_recovery_enabled: bool = Field(default=True)
    rival_history_recovery_seconds: int = Field(default=300, ge=300, le=21600)
    rival_history_audit_enabled: bool = Field(default=True)

    # Low-cost local research; never auto-promotes models or places orders.
    rival_shadow_learning_enabled: bool = Field(default=True)
    rival_shadow_eval_seconds: int = Field(default=3600, ge=900, le=21600)

    # Independent, user-triggered Gemini research (never called by candle worker).
    # Reads existing GEMINI_API_KEY in Discloud if configured there.
    rival_ai_provider: Literal["openrouter", "gemini", "disabled"] = Field(default="openrouter")
    open_router_api_key: SecretStr | None = Field(default=None)
    openrouter_api_key: SecretStr | None = Field(default=None)
    rival_openrouter_daily_max: int = Field(default=18, ge=1, le=18)
    rival_openrouter_cooldown_seconds: int = Field(default=3600, ge=60, le=86400)
    gemini_api_key: SecretStr | None = Field(default=None)
    google_api_key: SecretStr | None = Field(default=None)
    rival_gemini_model: str = Field(default="gemini-3.8-flash")
    rival_gemini_daily_max: int = Field(default=24, ge=1, le=96)
    rival_gemini_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    # AI reviewers use a shared free allowance. No owner request should
    # silently consume it; opt in explicitly to background LLM calls.
    rival_gemini_auto_enabled: bool = Field(default=False)

    # Official licensed Polymarket PolyBolt Chainlink TWAP (NOT free CLOB book).
    # Three CLOB credentials are used ONLY for the read-only live WS auth.
    # Never use a private wallet key or create/place any trading orders.
    rival_polybolt_enabled: bool = Field(default=True)
    polymarket_clob_api_key: SecretStr | None = Field(default=None)
    polymarket_clob_api_secret: SecretStr | None = Field(default=None)
    polymarket_clob_api_passphrase: SecretStr | None = Field(default=None)

    rival_drive_root_folder_id: str | None = Field(default=None)
    rival_drive_history_folder_id: str | None = Field(default=None)
    rival_drive_manifests_folder_id: str | None = Field(default=None)
    rival_drive_models_folder_id: str | None = Field(default=None)
    rival_drive_predictions_folder_id: str | None = Field(default=None)
    rival_drive_backups_folder_id: str | None = Field(default=None)
    rival_drive_reports_folder_id: str | None = Field(default=None)
    rival_drive_staging_folder_id: str | None = Field(default=None)

    rival_drive_oauth_client_id: str | None = Field(default=None)
    rival_drive_oauth_client_secret: SecretStr | None = Field(default=None)
    rival_drive_oauth_refresh_token: SecretStr | None = Field(default=None)
    rival_archive_autostart_when_configured: bool = Field(default=True)
    rival_archive_enabled: bool = Field(default=False)
    rival_archive_local_dir: str = Field(default="data/archive-staging")
    rival_archive_closed_state_minutes: int = Field(default=60, ge=10)
    rival_archive_batch_rows: int = Field(default=25_000, ge=100, le=250_000)
    rival_archive_chunk_mib: int = Field(default=8, ge=1, le=64)
    rival_archive_run_seconds: int = Field(default=3600, ge=300)
    rival_archive_max_batches: int = Field(default=4, ge=1, le=24)

    # Separate opt-in for old 5-minute perpetual candles, using the SAME
    # private Drive root and existing hourly archive loop. Copy-only.
    rival_perp_archive_enabled: bool = Field(default=False)
    rival_perp_archive_keep_hot_days: int = Field(default=14, ge=7, le=365)
    rival_perp_archive_batch_rows: int = Field(default=1000, ge=1, le=5000)
    rival_perp_archive_max_batches: int = Field(default=2, ge=1, le=4)
