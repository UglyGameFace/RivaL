"""Sports subscriptions and archiving must be opt-in, never crypto blockers."""
from __future__ import annotations

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient


def _settings(tmp_path, **options):
    return Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
        sportsgameodds_api_key="present-but-unused",
        oddspapi_api_key="present-but-unused",
        the_odds_api_key="present-but-unused",
        **options,
    )


def test_unrelated_sports_keys_do_not_start_any_provider(tmp_path):
    settings = _settings(tmp_path)
    assert settings.rival_sports_background_enabled is False
    assert settings.rival_sports_archive_enabled is False
    bot = RivalDiscordClient(settings)
    try:
        assert bot.current_provider is None
        assert bot.current_collector is None
        assert bot.sports_results is None
        assert bot.oddspapi_provider is None
        assert bot.oddspapi_collector is None
        assert bot.the_odds_api_collector is None
        # The crypto and Polymarket service objects remain independently
        # wired. No sports plan key, quota, or query is a prerequisite.
        assert bot.polymarket_board is not None
        assert bot.auto_hunt is not None
    finally:
        bot.store.close()


def test_sports_work_is_explicitly_enabled_not_key_presence(tmp_path):
    settings = _settings(tmp_path, rival_sports_background_enabled=True)
    bot = RivalDiscordClient(settings)
    try:
        assert bot.current_provider is not None
        assert bot.current_collector is not None
        assert bot.oddspapi_collector is not None
        assert bot.the_odds_api_collector is not None
    finally:
        bot.store.close()


def test_crypto_archive_and_sports_archive_flags_are_independent(tmp_path):
    settings = _settings(
        tmp_path, rival_sports_archive_enabled=False,
        rival_perp_archive_enabled=True, rival_archive_enabled=True,
    )
    assert settings.rival_sports_archive_enabled is False
    assert settings.rival_perp_archive_enabled is True
    future = settings.model_copy(update={"rival_sports_archive_enabled": True})
    assert future.rival_sports_archive_enabled is True
    assert future.rival_perp_archive_enabled is True
