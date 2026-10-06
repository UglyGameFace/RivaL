from __future__ import annotations

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient, build_hot_store, run_discord_bot
from parlay_bot.discord_app.presentation import (
    display_book,
    location_embed,
    onboarding_embed,
    parlay_embed,
)
from parlay_bot.discord_app.views import (
    LocationView,
    OnboardingView,
    ParlayResultView,
    StateModal,
    ZipModal,
)
from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService, OnboardingLocation
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.parlays.models import ParlayLeg, ParlaySlip, RiskMode
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
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
    )
    client = RivalDiscordClient(settings)
    try:
        command = client.tree.get_command("rival")
        assert command is not None
        assert command.description == "Open your private RivaL sports odds dashboard."
        assert client.intents.message_content is False
        assert client.archive_runtime is None
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


def build_parlay_builder(tmp_path) -> ParlayBuilder:
    return ParlayBuilder(
        store=SQLiteHotStore(tmp_path / "rival-parlay.sqlite"),
        registry=JurisdictionRegistry(verified_us_sportsbooks()),
    )


@pytest.mark.asyncio
async def test_location_view_exposes_build_parlay_when_engine_is_attached(tmp_path) -> None:
    service = build_service(tmp_path)
    builder = build_parlay_builder(tmp_path)
    view = LocationView(service=service, parlay_builder=builder)

    controls = {item.custom_id: item.label for item in view.children}

    assert controls["rival:parlay:build"] == "Build Parlay"
    assert controls["rival:location:change"] == "Change Location"
    assert controls["rival:location:refresh"] == "Refresh Books"


@pytest.mark.asyncio
async def test_parlay_result_view_only_exposes_working_v1_controls(tmp_path) -> None:
    service = build_service(tmp_path)
    builder = build_parlay_builder(tmp_path)
    view = ParlayResultView(service=service, parlay_builder=builder)

    controls = {item.custom_id: item.label for item in view.children}

    assert controls == {
        "rival:parlay:safer": "Make Safer",
        "rival:parlay:boost": "Boost Payout",
        "rival:parlay:back": "Back",
    }


def test_parlay_embed_labels_market_consensus_not_prediction_model() -> None:
    slip = ParlaySlip(
        bookmaker="fanduel",
        risk_mode=RiskMode.BALANCED,
        legs=[
            ParlayLeg(
                fixture_id="fixture-1",
                event_name="Home vs Away",
                bookmaker="fanduel",
                market_id="100",
                market_name="Winner",
                outcome_id="101",
                outcome_name="Home",
                player_id="0",
                price_decimal=2.02,
                price_american="+102",
                market_fair_probability=0.51,
                price_edge=0.0302,
                reference_books=3,
            ),
            ParlayLeg(
                fixture_id="fixture-2",
                event_name="Team C vs Team D",
                bookmaker="fanduel",
                market_id="100",
                market_name="Winner",
                outcome_id="101",
                outcome_name="Home",
                player_id="0",
                price_decimal=1.95,
                price_american="-105",
                market_fair_probability=0.53,
                price_edge=0.0335,
                reference_books=3,
            ),
        ],
        combined_decimal_odds=3.939,
        market_fair_probability=0.2703,
        market_implied_edge=0.0649,
    )

    rendered = parlay_embed(slip).to_dict()

    assert "FanDuel" in rendered["title"]
    assert "Market-implied hit estimate" in rendered["fields"][0]["value"]
    assert "one leg per fixture" in rendered["fields"][1]["value"].lower()
    assert "not guaranteed outcomes" in rendered["footer"]["text"]
    assert "not" in rendered["footer"]["text"].lower()


@pytest.mark.asyncio
async def test_discord_client_configures_current_collector_without_polling_on_startup(
    tmp_path,
) -> None:
    settings = Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
        sportsgameodds_api_key="test-secret",
        rival_current_leagues="NBA,NFL",
        rival_current_bookmakers="draftkings,fanduel",
    )
    client = RivalDiscordClient(settings)
    try:
        assert client.current_provider is not None
        assert client.current_collector is not None
        assert client.current_collector.league_ids == ("NBA", "NFL")
        assert client.current_collector.bookmaker_ids == ("draftkings", "fanduel")
        assert client.current_collector.is_due() is True
    finally:
        if client.current_provider is not None:
            await client.current_provider.aclose()
        await client.zip_resolver.aclose()


def test_production_storage_requires_database_url() -> None:
    settings = Settings(_env_file=None, rival_storage_backend="postgres")

    with pytest.raises(RuntimeError, match="RIVAL_DATABASE_URL"):
        build_hot_store(settings)


def test_explicit_sqlite_backend_remains_available_for_local_tests(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
    )

    store = build_hot_store(settings)
    try:
        assert isinstance(store, SQLiteHotStore)
        store.initialize()
    finally:
        store.close()
