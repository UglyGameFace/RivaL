from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    oddspapi_api_key: SecretStr | None = Field(default=None)
    oddspapi_base_url: str = Field(default="https://api.oddspapi.io")
    oddspapi_quota_reserve: int = Field(default=20, ge=0)
    oddspapi_account_cache_seconds: int = Field(default=60, ge=0)

    sportsgameodds_api_key: SecretStr | None = Field(default=None)
    sportsgameodds_base_url: str = Field(default="https://api.sportsgameodds.com/v2")
    sportsgameodds_monthly_entity_reserve: int = Field(default=250, ge=0)
    rival_current_leagues: str = Field(default="NBA,NFL")
    rival_current_bookmakers: str = Field(default="draftkings,fanduel")
    rival_current_refresh_seconds: int = Field(default=600, ge=60)
    rival_current_event_limit: int = Field(default=25, ge=1, le=100)
    rival_current_max_pages: int = Field(default=1, ge=1, le=5)

    rival_db_path: str = Field(default="data/rival.sqlite")
    discord_token: SecretStr | None = Field(default=None)
    rival_dev_guild_id: int | None = Field(default=None, ge=1)

    rival_drive_root_folder_id: str | None = Field(default=None)
    rival_drive_history_folder_id: str | None = Field(default=None)
    rival_drive_manifests_folder_id: str | None = Field(default=None)
    rival_drive_models_folder_id: str | None = Field(default=None)
    rival_drive_predictions_folder_id: str | None = Field(default=None)
    rival_drive_backups_folder_id: str | None = Field(default=None)
    rival_drive_reports_folder_id: str | None = Field(default=None)
    rival_drive_staging_folder_id: str | None = Field(default=None)
