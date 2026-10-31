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


def test_legacy_background_ai_flag_cannot_automatically_opt_in(monkeypatch):
    monkeypatch.setenv("RIVAL_GEMINI_AUTO_ENABLED", "true")
    monkeypatch.delenv("RIVAL_AI_BACKGROUND_OPT_IN", raising=False)
    settings = Settings(_env_file=None)
    assert settings.rival_gemini_auto_enabled is True
    assert settings.rival_ai_background_opt_in is False
    assert not (
        settings.rival_gemini_auto_enabled
        and settings.rival_ai_background_opt_in
    )
    monkeypatch.setenv("RIVAL_AI_BACKGROUND_OPT_IN", "true")
    enabled = Settings(_env_file=None)
    assert enabled.rival_ai_background_opt_in is True
