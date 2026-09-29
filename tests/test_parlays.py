from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.domain.catalog import MarketDefinition
from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard
from parlay_bot.jurisdiction.models import SportsbookAvailability
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.parlays.math import no_vig_probabilities
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from parlay_bot.storage.catalog import MarketCatalogStore
from parlay_bot.storage.hot import SQLiteHotStore


def catalog() -> list[MarketDefinition]:
    return [
        MarketDefinition.model_validate(
            {
                "marketId": "100",
                "marketLength": 2,
                "marketName": "Winner",
                "playerProp": False,
                "sportId": 11,
                "handicap": 0,
                "period": "fulltime",
                "marketType": "moneyline",
                "outcomes": [
                    {"outcomeId": "101", "outcomeName": "Home"},
                    {"outcomeId": "102", "outcomeName": "Away"},
                ],
            }
        )
    ]


def registry() -> JurisdictionRegistry:
    return JurisdictionRegistry(
        [
            SportsbookAvailability(
                jurisdiction="CT",
                sportsbook="draftkings",
                online_available=True,
            ),
            SportsbookAvailability(
                jurisdiction="CT",
                sportsbook="fanduel",
                online_available=True,
            ),
        ]
    )


def observation(
    *,
    fixture: str,
    bookmaker: str,
    outcome: str,
    price: float,
    changed: datetime,
) -> MarketObservation:
    return MarketObservation(
        provider="test",
        fixture_id=fixture,
        sport_id=11,
        tournament_id=132,
        bookmaker=bookmaker,
        market_id="100",
        outcome_id=outcome,
        player_id="0",
        active=True,
        main_line=True,
        price_decimal=price,
        changed_at=changed,
    )


def seed_fixture(
    store: SQLiteHotStore,
    *,
    number: int,
    dk_home: float,
    dk_away: float,
    fd_home: float,
    fd_away: float,
) -> None:
    now = datetime.now(UTC) + timedelta(minutes=number)
    store.ingest_board(
        NormalizedOddsBoard(
            provider="test",
            fixture_id=f"fixture-{number}",
            sport_id=11,
            tournament_id=132,
            status_id=0,
            status_name="Pre-Game",
            start_time=now + timedelta(days=1),
            participant1_name=f"Home {number}",
            participant2_name=f"Away {number}",
            sport_name="Basketball",
            tournament_name="NBA",
            observations=[
                observation(
                    fixture=f"fixture-{number}",
                    bookmaker="draftkings",
                    outcome="101",
                    price=dk_home,
                    changed=now,
                ),
                observation(
                    fixture=f"fixture-{number}",
                    bookmaker="draftkings",
                    outcome="102",
                    price=dk_away,
                    changed=now,
                ),
                observation(
                    fixture=f"fixture-{number}",
                    bookmaker="fanduel",
                    outcome="101",
                    price=fd_home,
                    changed=now,
                ),
                observation(
                    fixture=f"fixture-{number}",
                    bookmaker="fanduel",
                    outcome="102",
                    price=fd_away,
                    changed=now,
                ),
            ],
        )
    )


def build_engine(tmp_path) -> tuple[ParlayBuilder, SQLiteHotStore]:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    labels = MarketCatalogStore(store.path)
    labels.replace(catalog())
    engine = ParlayBuilder(store=store, registry=registry(), catalog=labels)
    return engine, store


def test_no_vig_probabilities_remove_overround() -> None:
    fair = no_vig_probabilities([1.91, 1.91])
    assert fair[0] == pytest.approx(0.5)
    assert fair[1] == pytest.approx(0.5)
    assert sum(fair) == pytest.approx(1.0)


def test_builds_one_book_slip_with_unique_fixtures_and_labels(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    for number in range(1, 4):
        seed_fixture(
            store,
            number=number,
            dk_home=1.91,
            dk_away=1.91,
            fd_home=2.02,
            fd_away=1.86,
        )

    slip = engine.build_for_state(state_code="CT", leg_count=3, risk=RiskMode.BALANCED)

    assert len(slip.legs) == 3
    assert len({leg.fixture_id for leg in slip.legs}) == 3
    assert len({leg.bookmaker for leg in slip.legs}) == 1
    assert slip.bookmaker == "fanduel"
    assert all(leg.market_name == "Winner" for leg in slip.legs)
    assert all(leg.outcome_name == "Home" for leg in slip.legs)
    assert slip.correlation_policy == "one_leg_per_fixture"
    assert slip.combined_decimal_odds > 1


def test_user_build_requires_saved_jurisdiction(tmp_path) -> None:
    engine, _store = build_engine(tmp_path)

    with pytest.raises(ParlayBuildError, match="Set your betting state"):
        engine.build_for_user(platform="discord", user_id="123")


def test_user_build_uses_saved_state_and_never_crosses_books(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    store.set_user_jurisdiction(
        platform="discord",
        user_id="123",
        state_code="CT",
        source="state",
    )
    for number in range(1, 4):
        seed_fixture(
            store,
            number=number,
            dk_home=1.91,
            dk_away=1.91,
            fd_home=2.02,
            fd_away=1.86,
        )

    slip = engine.build_for_user(platform="discord", user_id="123")

    assert slip.bookmaker in {"draftkings", "fanduel"}
    assert {leg.bookmaker for leg in slip.legs} == {slip.bookmaker}


def test_rejects_same_fixture_as_multiple_legs(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    seed_fixture(
        store,
        number=1,
        dk_home=1.91,
        dk_away=1.91,
        fd_home=2.02,
        fd_away=1.86,
    )

    with pytest.raises(ParlayBuildError, match="independent fixtures"):
        engine.build_for_state(state_code="CT", leg_count=2)


def test_stale_prices_are_not_used_for_new_slips(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    stale_time = datetime.now(UTC) - timedelta(hours=2)
    fixture_time = datetime.now(UTC) + timedelta(days=1)
    store.ingest_board(
        NormalizedOddsBoard(
            provider="test",
            fixture_id="stale-fixture",
            sport_id=11,
            tournament_id=132,
            start_time=fixture_time,
            participant1_name="Old Home",
            participant2_name="Old Away",
            observations=[
                observation(
                    fixture="stale-fixture",
                    bookmaker="draftkings",
                    outcome="101",
                    price=1.91,
                    changed=stale_time,
                ).model_copy(update={"observed_at": stale_time}),
                observation(
                    fixture="stale-fixture",
                    bookmaker="draftkings",
                    outcome="102",
                    price=1.91,
                    changed=stale_time,
                ).model_copy(update={"observed_at": stale_time}),
            ],
        )
    )

    with pytest.raises(ParlayBuildError, match="No current eligible sportsbook prices"):
        engine.build_for_state(state_code="CT", leg_count=2)


def seed_spread_fixture(
    store: SQLiteHotStore,
    *,
    number: int,
    dk_line: float,
    fd_line: float,
) -> None:
    now = datetime.now(UTC) + timedelta(minutes=number)
    observations: list[MarketObservation] = []
    for bookmaker, line in (("draftkings", dk_line), ("fanduel", fd_line)):
        group = abs(line)
        observations.extend(
            [
                MarketObservation(
                    provider="test",
                    fixture_id=f"spread-{number}",
                    sport_id=11,
                    tournament_id=132,
                    bookmaker=bookmaker,
                    market_id="spread-pair",
                    market_name="Spread",
                    outcome_id="home",
                    outcome_name="Home",
                    player_id="0",
                    line_value=line,
                    line_group_value=group,
                    active=True,
                    main_line=True,
                    price_decimal=1.91,
                    changed_at=now,
                ),
                MarketObservation(
                    provider="test",
                    fixture_id=f"spread-{number}",
                    sport_id=11,
                    tournament_id=132,
                    bookmaker=bookmaker,
                    market_id="spread-pair",
                    market_name="Spread",
                    outcome_id="away",
                    outcome_name="Away",
                    player_id="0",
                    line_value=-line,
                    line_group_value=group,
                    active=True,
                    main_line=True,
                    price_decimal=1.91,
                    changed_at=now,
                ),
            ]
        )

    store.ingest_board(
        NormalizedOddsBoard(
            provider="test",
            fixture_id=f"spread-{number}",
            sport_id=11,
            tournament_id=132,
            start_time=now + timedelta(days=1),
            participant1_name=f"Home {number}",
            participant2_name=f"Away {number}",
            observations=observations,
        )
    )


def test_mismatched_spreads_are_not_falsely_compared_as_consensus(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    seed_spread_fixture(store, number=1, dk_line=-5.5, fd_line=-6.0)
    seed_spread_fixture(store, number=2, dk_line=-5.5, fd_line=-6.0)

    with pytest.raises(ParlayBuildError, match="No current eligible sportsbook prices"):
        engine.build_for_state(state_code="CT", leg_count=2)
