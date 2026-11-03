"""Prevent accidental reactivation of retired external AI inference."""
from pathlib import Path

from parlay_bot.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_external_ai_adapters_are_not_shipped():
    for name in (
        "gemini_research.py",
        "openrouter_research.py",
        "cloudflare_research.py",
    ):
        assert not (ROOT / "src" / "parlay_bot" / "perpetuals" / name).exists()


def test_bot_only_instantiates_private_local_intelligence():
    code = (ROOT / "src/parlay_bot/discord_app/bot.py").read_text()
    assert "LocalFirstResearch(" in code
    assert "OpenRouterResearch" not in code
    assert "CloudflareResearch" not in code
    assert "GeminiResearch" not in code
    assert "self.intelligence_research" in code


def test_retired_api_keys_do_not_enter_settings(monkeypatch):
    for name in (
        "OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        "RIVAL_CLOUDFLARE_AI_TOKEN",
    ):
        monkeypatch.setenv(name, "unused-provider-secret")
    settings = Settings(_env_file=None)
    for attr in (
        "open_router_api_key", "openrouter_api_key", "gemini_api_key",
        "rival_cloudflare_ai_token",
    ):
        assert not hasattr(settings, attr)
