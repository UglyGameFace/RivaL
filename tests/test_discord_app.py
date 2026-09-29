from __future__ import annotations

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient, run_discord_bot
from parlay_bot.discord_app.presentation import (
    display_book,
    location_embed,
    onboarding_embed,
)
from parlay_bot.discord_app.views import LocationView, OnboardingView, StateModal, ZipModal
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService, OnboardingLocation
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.storage.hot import SQLiteHotStore


class NoNetworkZipResolver:
    async def resolve(self, _zip_code: str) -> str:
        return "CT"


def build_service(tmp_path) -> LocationOnboardingService:
    return LocationOnboardingService(
        store=SQLiteHotStore(tmp_path / "rival.sqlite"),
        registry=JurisdictionRegistry(verified_us_sportsbooks()),
        zip_resolver=NoNetworkZipResolver(),
    )


def test_onboarding_embed_explains_zip_privacy() -> None:
    embed = onboarding_embed()
    rendered = embed.to_dict()

    assert rendered["title"] == "RivaL"
    assert "saves only the state" in rendered["fields"][0]["value"]
    assert "does not verify your physical location" in rendered["footer"]["text"]


def test_location_embed_formats_verified_books() -> None:
    embed = location_embed(
        OnboardingLocation(
            state_code="CT",
            state_name="Connecticut",
            eligible_books=("draftkings", "fanduel", "fanatics"),
            source="state",
        )
    )
    rendered = embed.to_dict()
    books = rendered["fields"][0]["value"]

    assert "DraftKings" in books
    assert "FanDuel" in books
    assert "Fanatics Sportsbook" in books
    assert display_book("hardrockbet") == "Hard Rock Bet"


@pytest.mark.asyncio
async def test_onboarding_view_has_only_working_location_entry_buttons(tmp_path) -> None:
    service = build_service(tmp_path)
    view = OnboardingView(service=service)

    controls = {item.custom_id: item.label for item in view.children}

    assert controls == {
        "rival:onboarding:zip": "Enter ZIP",
        "rival:onboarding:state": "Choose State",
    }


@pytest.mark.asyncio
async def test_location_view_has_no_dead_parlay_button(tmp_path) -> None:
    service = build_service(tmp_path)
    view = LocationView(service=service)

    controls = {item.custom_id: item.label for item in view.children}

    assert controls == {
        "rival:location:change": "Change Location",
        "rival:location:refresh": "Refresh Books",
    }


@pytest.mark.asyncio
async def test_modals_enforce_location_input_bounds(tmp_path) -> None:
    service = build_service(tmp_path)

    zip_modal = ZipModal(service=service)
    state_modal = StateModal(service=service)

    assert zip_modal.zip_code.min_length == 5
    assert zip_modal.zip_code.max_length == 5
    assert state_modal.state.min_length == 2
    assert state_modal.state.max_length == 32


@pytest.mark.asyncio
async def test_discord_client_registers_one_rival_command_without_message_intent(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
    )
    client = RivalDiscordClient(settings)
    try:
        command = client.tree.get_command("rival")
        assert command is not None
        assert command.description == "Open your private RivaL sports odds dashboard."
        assert client.intents.message_content is False
    finally:
        await client.zip_resolver.aclose()


def test_run_requires_discord_token(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
    )

    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        run_discord_bot(settings)
