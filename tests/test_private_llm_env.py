"""Discloud LLM accepts private runtime credentials, not public artifacts."""
from __future__ import annotations

import os
import runpy
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "services" / "rival-llm"
sys.path.insert(0, str(ROOT))
LAUNCHER = runpy.run_path(str(ROOT / "main.py"))
NAME = "RIVAL_LLM_SHARED_TOKEN"


def test_private_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(NAME, "a" * 48)
    (tmp_path / ".env").write_text(f"{NAME}={'b' * 48}\n")
    LAUNCHER["load_optional_local_secret"]()
    assert os.environ[NAME] == "a" * 48


def test_private_dotenv_can_be_loaded_without_pip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(NAME, raising=False)
    path = tmp_path / ".env"
    path.write_text("# Private file\n" + NAME + "=" + "secret_" + "a" * 42 + "\n")
    path.chmod(0o644)
    LAUNCHER["load_optional_local_secret"]()
    assert os.environ[NAME] == "secret_" + "a" * 42
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ["not-long-enough", "a" * 280, "a" * 35 + "!", ""])
def test_private_dotenv_rejects_weak_or_malformed_secrets(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(NAME, raising=False)
    (tmp_path / ".env").write_text(NAME + "=" + value + "\n")
    with pytest.raises(ValueError, match="invalid"):
        LAUNCHER["load_optional_local_secret"]()
    assert not os.getenv(NAME)


def test_duplicate_private_token_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(NAME, raising=False)
    (tmp_path / ".env").write_text(f"{NAME}={'a' * 48}\n{NAME}={'b' * 48}\n")
    with pytest.raises(ValueError, match="Duplicate"):
        LAUNCHER["load_optional_local_secret"]()


def test_private_file_symlinks_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(NAME, raising=False)
    target = tmp_path / "other.txt"
    target.write_text(NAME + "=" + "a" * 48)
    (tmp_path / ".env").symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        LAUNCHER["load_optional_local_secret"]()


def test_no_env_file_does_not_create_a_secret(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(NAME, raising=False)
    LAUNCHER["load_optional_local_secret"]()
    assert not os.getenv(NAME)
    assert not (tmp_path / ".env").exists()


def test_large_env_file_rejected_without_reading(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(NAME, raising=False)
    (tmp_path / ".env").write_text("#" * 5000)
    with pytest.raises(ValueError, match="size limit"):
        LAUNCHER["load_optional_local_secret"]()
