"""Blank environment variables must mean "unset", not a validation error.

.env.example ships RIVAL_DEV_GUILD_ID= blank. Without env_ignore_empty,
pydantic-settings passes "" to the int field and Settings() raises
int_parsing before the bot starts.
"""

from __future__ import annotations

import pytest

from parlay_bot.config import Settings


def test_blank_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVAL_DEV_GUILD_ID", "")
    monkeypatch.setenv("RIVAL_DATABASE_URL", "")
    monkeypatch.setenv("DISCORD_TOKEN", "")
    settings = Settings(_env_file=None)
    assert settings.rival_dev_guild_id is None
    assert settings.rival_database_url is None
    assert settings.discord_token is None


def test_nonempty_env_vars_still_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVAL_DEV_GUILD_ID", "123456789")
    settings = Settings(_env_file=None)
    assert settings.rival_dev_guild_id == 123456789


def test_retired_provider_env_is_ignored_by_settings(monkeypatch):
    monkeypatch.setenv("RIVAL_AI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "retired")
    monkeypatch.setenv("RIVAL_GEMINI_AUTO_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.rival_ai_provider == "openrouter"
    assert not hasattr(settings, "open_router_api_key")
    assert not hasattr(settings, "rival_gemini_auto_enabled")


def test_new_install_defaults_to_private_llm(monkeypatch):
    monkeypatch.delenv("RIVAL_AI_PROVIDER", raising=False)
    assert Settings(_env_file=None).rival_ai_provider == "local"
