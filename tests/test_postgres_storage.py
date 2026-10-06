from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.archive.queue import RelationalArchiveQueue
from parlay_bot.domain.catalog import MarketDefinition
from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard
from parlay_bot.ingestion.sportsgameodds import normalize_event
from parlay_bot.jurisdiction.models import SportsbookAvailability
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.storage.catalog import MarketCatalogStore
from parlay_bot.storage.hot import PostgresHotStore
from tests.sgo_samples import sample_sgo_event


@pytest.fixture
def postgres_store() -> PostgresHotStore:
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("RIVAL_TEST_DATABASE_URL is not configured")

    store = PostgresHotStore(dsn)
    with store.connect() as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS outcome_catalog CASCADE;
            DROP TABLE IF EXISTS market_catalog CASCADE;
            DROP TABLE IF EXISTS archive_batches CASCADE;
            DROP TABLE IF EXISTS runtime_state CASCADE;
            DROP TABLE IF EXISTS user_jurisdictions CASCADE;
            DROP TABLE IF EXISTS current_odds CASCADE;
            DROP TABLE IF EXISTS odds_changes CASCADE;
            DROP TABLE IF EXISTS fixtures CASCADE;
            """
        )
    store.initialize()
    try:
        yield store
    finally:
        with store.connect() as connection:
            connection.executescript(
                """
                DROP TABLE IF EXISTS outcome_catalog CASCADE;
                DROP TABLE IF EXISTS market_catalog CASCADE;
                DROP TABLE IF EXISTS archive_batches CASCADE;
                DROP TABLE IF EXISTS runtime_state CASCADE;
                DROP TABLE IF EXISTS user_jurisdictions CASCADE;
                DROP TABLE IF EXISTS current_odds CASCADE;
                DROP TABLE IF EXISTS odds_changes CASCADE;
                DROP TABLE IF EXISTS fixtures CASCADE;
                """
            )
        store.close()


def test_postgres_ingests_sportsgameodds_string_ids_and_shared_state(
    postgres_store: PostgresHotStore,
) -> None:
    board = normalize_event(sample_sgo_event())

    first = postgres_store.ingest_board(board)
    second = postgres_store.ingest_board(board)

    assert first.observations_seen == 6
    assert first.new_states == 6
    assert second.new_states == 0
    assert second.repeated_states == 6
    assert len(postgres_store.current_for_fixture(board.fixture_id)) == 6

    with postgres_store.connect() as connection:
        fixture = connection.execute(
            """
            SELECT sport_id, tournament_id
            FROM fixtures
            WHERE provider = ? AND fixture_id = ?
            """,
            (board.provider, board.fixture_id),
        ).fetchone()

    assert fixture is not None
    assert fixture["sport_id"] == str(board.sport_id)
    assert fixture["tournament_id"] == str(board.tournament_id)

    postgres_store.set_runtime_state("collector:last_refresh", "2026-10-04T20:00:00+00:00")
    assert postgres_store.get_runtime_state("collector:last_refresh") == "2026-10-04T20:00:00+00:00"

    postgres_store.set_user_jurisdiction(
        platform="discord",
        user_id="123",
        state_code="CT",
        source="state",
    )
    saved = postgres_store.get_user_jurisdiction(platform="discord", user_id="123")
    assert saved is not None
    assert saved["state_code"] == "CT"
    assert saved["source"] == "state"


def test_market_catalog_uses_same_postgres_database(
    postgres_store: PostgresHotStore,
) -> None:
    catalog = MarketCatalogStore(postgres_store)
    catalog.replace(
        [
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
    )

    metadata = catalog.metadata()

    assert metadata["100"]["market_name"] == "Winner"
    assert metadata["100"]["sport_id"] == "11"
    assert metadata["100"]["outcomes"] == {"101": "Home", "102": "Away"}


def _archive_observation(*, price: float, changed_at: datetime) -> MarketObservation:
    return MarketObservation(
        provider="test",
        fixture_id="fixture-archive",
        sport_id="BASKETBALL",
        tournament_id="NBA",
        bookmaker="draftkings",
        market_id="winner",
        market_name="Winner",
        outcome_id="home",
        outcome_name="Home",
        player_id="0",
        active=True,
        main_line=True,
        price_decimal=price,
        price_american="-110",
        changed_at=changed_at,
        observed_at=changed_at,
    )


def test_postgres_archive_queue_preserves_latest_and_commits_transactionally(
    postgres_store: PostgresHotStore,
) -> None:
    now = datetime.now(UTC)
    for price, changed_at in (
        (1.91, now - timedelta(hours=3)),
        (2.00, now - timedelta(hours=2)),
    ):
        postgres_store.ingest_board(
            NormalizedOddsBoard(
                provider="test",
                fixture_id="fixture-archive",
                sport_id="BASKETBALL",
                tournament_id="NBA",
                start_time=now + timedelta(days=1),
                participant1_name="Home",
                participant2_name="Away",
                sport_name="Basketball",
                tournament_name="NBA",
                observations=[
                    _archive_observation(price=price, changed_at=changed_at)
                ],
            )
        )

    queue = RelationalArchiveQueue(postgres_store)
    batch = queue.next_batch(cutoff=now - timedelta(hours=1), max_rows=100)

    assert batch is not None
    assert batch.row_count == 1
    changes = postgres_store.changes_for_selection(batch.rows[0]["selection_key"])
    assert len(changes) == 2
    assert batch.row_ids == (changes[0]["id"],)
    assert changes[1]["id"] not in batch.row_ids

    queue.set_history_file(batch.batch_id, "history-file")
    with pytest.raises(RuntimeError, match="history and manifest"):
        queue.mark_complete(
            batch_id=batch.batch_id,
            row_ids=batch.row_ids,
            completed_at=now,
        )

    queue.set_manifest_file(batch.batch_id, "manifest-file")
    queue.mark_complete(
        batch_id=batch.batch_id,
        row_ids=batch.row_ids,
        completed_at=now,
    )

    record = queue.batch_record(batch.batch_id)
    assert record is not None
    assert record["status"] == "complete"

    archived_changes = postgres_store.changes_for_selection(
        batch.rows[0]["selection_key"]
    )
    assert archived_changes[0]["archived_batch_id"] == batch.batch_id
    assert archived_changes[1]["archived_batch_id"] is None



def _parlay_registry() -> JurisdictionRegistry:
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


def _seed_postgres_parlay_fixture(
    store: PostgresHotStore,
    *,
    number: int,
) -> None:
    observed = datetime.now(UTC)
    fixture_id = f"pg-parlay-{number}"
    observations: list[MarketObservation] = []
    for bookmaker, home_price, away_price in (
        ("draftkings", 1.91, 1.91),
        ("fanduel", 2.02, 1.86),
    ):
        observations.extend(
            [
                MarketObservation(
                    provider="test",
                    fixture_id=fixture_id,
                    sport_id="BASKETBALL",
                    tournament_id="NBA",
                    bookmaker=bookmaker,
                    market_id="winner",
                    market_name="Winner",
                    outcome_id="home",
                    outcome_name="Home",
                    player_id="0",
                    active=True,
                    main_line=True,
                    price_decimal=home_price,
                    changed_at=observed,
                    observed_at=observed,
                ),
                MarketObservation(
                    provider="test",
                    fixture_id=fixture_id,
                    sport_id="BASKETBALL",
                    tournament_id="NBA",
                    bookmaker=bookmaker,
                    market_id="winner",
                    market_name="Winner",
                    outcome_id="away",
                    outcome_name="Away",
                    player_id="0",
                    active=True,
                    main_line=True,
                    price_decimal=away_price,
                    changed_at=observed,
                    observed_at=observed,
                ),
            ]
        )

    store.ingest_board(
        NormalizedOddsBoard(
            provider="test",
            fixture_id=fixture_id,
            sport_id="BASKETBALL",
            tournament_id="NBA",
            start_time=observed + timedelta(days=1),
            participant1_name=f"Home {number}",
            participant2_name=f"Away {number}",
            sport_name="Basketball",
            tournament_name="NBA",
            observations=observations,
        )
    )


def test_postgres_parlay_builder_queries_the_managed_store(
    postgres_store: PostgresHotStore,
) -> None:
    for number in range(1, 4):
        _seed_postgres_parlay_fixture(postgres_store, number=number)

    builder = ParlayBuilder(
        store=postgres_store,
        registry=_parlay_registry(),
    )
    slip = builder.build_for_state(state_code="CT", leg_count=3)

    assert slip.bookmaker == "fanduel"
    assert len(slip.legs) == 3
    assert {leg.fixture_id for leg in slip.legs} == {
        "pg-parlay-1",
        "pg-parlay-2",
        "pg-parlay-3",
    }
    assert {leg.bookmaker for leg in slip.legs} == {"fanduel"}
