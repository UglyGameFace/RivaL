from datetime import UTC, datetime

from parlay_bot.ingestion.oddspapi import normalize_current_odds
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_ingestion import sample_payload


def test_repeated_board_updates_occurrence_without_duplicate_change(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    payload = sample_payload()
    first_board = normalize_current_odds(
        payload,
        observed_at=datetime(2026, 9, 28, 20, 1, tzinfo=UTC),
    )
    second_board = normalize_current_odds(
        payload,
        observed_at=datetime(2026, 9, 28, 20, 2, tzinfo=UTC),
    )

    first = store.ingest_board(first_board)
    second = store.ingest_board(second_board)

    assert first.new_states == 2
    assert second.new_states == 0
    assert second.repeated_states == 2

    selection = first_board.observations[0]
    changes = store.changes_for_selection(selection.selection_key)
    assert len(changes) == 1
    assert changes[0]["occurrences"] == 2
    assert changes[0]["first_seen"].endswith("+00:00")
    assert changes[0]["last_seen"].endswith("+00:00")


def test_price_change_appends_history_and_updates_current(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    first_payload = sample_payload()
    first_board = normalize_current_odds(
        first_payload,
        observed_at=datetime(2026, 9, 28, 20, 1, tzinfo=UTC),
    )
    store.ingest_board(first_board)

    second_payload = sample_payload()
    player = second_payload["bookmakerOdds"]["fanduel"]["markets"]["100"]["outcomes"]["101"][
        "players"
    ]["0"]
    player["price"] = 2.0
    player["priceAmerican"] = "+100"
    player["changedAt"] = "2026-09-28T20:03:00Z"

    second_board = normalize_current_odds(
        second_payload,
        observed_at=datetime(2026, 9, 28, 20, 4, tzinfo=UTC),
    )
    result = store.ingest_board(second_board)

    selection = second_board.observations[0]
    changes = store.changes_for_selection(selection.selection_key)
    current = store.current_for_fixture("fixture-1")
    current_standard = next(row for row in current if row["market_id"] == "100")

    assert result.new_states == 1
    assert len(changes) == 2
    assert current_standard["price_decimal"] == 2.0
    assert current_standard["price_american"] == "+100"


def test_stale_provider_update_does_not_replace_current_price(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")

    newer_payload = sample_payload()
    newer = newer_payload["bookmakerOdds"]["fanduel"]["markets"]["100"]["outcomes"]["101"][
        "players"
    ]["0"]
    newer["price"] = 2.1
    newer["changedAt"] = "2026-09-28T20:05:00Z"
    store.ingest_board(normalize_current_odds(newer_payload))

    stale_payload = sample_payload()
    stale = stale_payload["bookmakerOdds"]["fanduel"]["markets"]["100"]["outcomes"]["101"][
        "players"
    ]["0"]
    stale["price"] = 1.8
    stale["changedAt"] = "2026-09-28T20:00:00Z"
    stale_result = store.ingest_board(normalize_current_odds(stale_payload))

    current = store.current_for_fixture("fixture-1")
    current_standard = next(row for row in current if row["market_id"] == "100")

    selection_key = normalize_current_odds(newer_payload).observations[0].selection_key
    changes = store.changes_for_selection(selection_key)

    assert current_standard["price_decimal"] == 2.1
    assert stale_result.stale_states_ignored == 1
    assert len(changes) == 1


def test_line_metadata_is_persisted_and_line_move_creates_new_state(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    payload = sample_payload()
    board = normalize_current_odds(payload)
    original = board.observations[0].model_copy(
        update={
            "market_name": "Spread",
            "outcome_name": "Home",
            "line_value": -5.5,
            "line_group_value": 5.5,
            "deeplink": "https://example.invalid/book",
        }
    )
    first = board.model_copy(update={"observations": [original]})
    store.ingest_board(first)

    moved = original.model_copy(
        update={
            "line_value": -6.0,
            "line_group_value": 6.0,
            "changed_at": original.changed_at + __import__("datetime").timedelta(minutes=1),
        }
    )
    store.ingest_board(board.model_copy(update={"observations": [moved]}))

    current = store.current_for_fixture(board.fixture_id)
    changes = store.changes_for_selection(original.selection_key)

    assert current[0]["market_name"] == "Spread"
    assert current[0]["outcome_name"] == "Home"
    assert current[0]["line_value"] == -6.0
    assert current[0]["line_group_value"] == 6.0
    assert current[0]["deeplink"] == "https://example.invalid/book"
    assert len(changes) == 2
