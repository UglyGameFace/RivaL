from datetime import UTC, datetime

import pytest

from parlay_bot.ingestion.sportsgameodds import american_to_decimal, normalize_event
from tests.sgo_samples import sample_sgo_event


def test_american_to_decimal_handles_positive_and_negative_prices() -> None:
    assert american_to_decimal("-110") == pytest.approx(1.9090909)
    assert american_to_decimal("+105") == pytest.approx(2.05)


def test_normalizes_spread_pair_with_shared_line_group() -> None:
    board = normalize_event(
        sample_sgo_event(),
        observed_at=datetime(2099, 9, 28, 20, 1, tzinfo=UTC),
    )

    assert board.provider == "sportsgameodds"
    assert board.fixture_id == "sgo-event-1"
    assert board.tournament_id == "NBA"
    assert board.participant1_name == "Boston Celtics"
    assert board.participant2_name == "New York Knicks"

    dk_spreads = [
        item
        for item in board.observations
        if item.bookmaker == "draftkings" and item.market_name == "Spread"
    ]
    assert len(dk_spreads) == 2
    assert len({item.market_id for item in dk_spreads}) == 1

    home = next(item for item in dk_spreads if item.outcome_id.endswith("-home"))
    away = next(item for item in dk_spreads if item.outcome_id.endswith("-away"))

    assert home.line_value == -5.5
    assert away.line_value == 5.5
    assert home.line_group_value == 5.5
    assert away.line_group_value == 5.5
    assert home.price_decimal == pytest.approx(1.9090909)
    assert home.deeplink == "https://example.invalid/dk/home"


def test_player_prop_preserves_player_and_total_line() -> None:
    board = normalize_event(sample_sgo_event())
    props = [
        item
        for item in board.observations
        if item.market_name == "Player Points"
    ]

    assert len(props) == 2
    assert {item.player_id for item in props} == {"player-1"}
    assert {item.player_name for item in props} == {"Example Player"}
    assert {item.line_value for item in props} == {27.5}
    assert {item.line_group_value for item in props} == {27.5}
    assert {item.outcome_name for item in props} == {"Over", "Under"}
