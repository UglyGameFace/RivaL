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
