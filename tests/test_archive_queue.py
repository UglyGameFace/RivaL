from datetime import UTC, datetime, timedelta

from parlay_bot.archive.queue import SQLiteArchiveQueue
from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard
from parlay_bot.storage.hot import SQLiteHotStore


def _observation(*, price: float, changed_at: datetime) -> MarketObservation:
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


def _seed_closed_state(path) -> tuple[SQLiteHotStore, datetime]:
    store = SQLiteHotStore(path)
    now = datetime.now(UTC)
    first_time = now - timedelta(hours=3)
    second_time = now - timedelta(hours=2)

    for price, changed in ((1.91, first_time), (2.0, second_time)):
        store.ingest_board(
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
                observations=[_observation(price=price, changed_at=changed)],
            )
        )
    return store, now


def test_archive_queue_selects_closed_state_but_never_latest(tmp_path) -> None:
    store, now = _seed_closed_state(tmp_path / "rival.sqlite")
    queue = SQLiteArchiveQueue(store.path)

    batch = queue.next_batch(cutoff=now - timedelta(hours=1), max_rows=100)

    assert batch is not None
    assert batch.row_count == 1
    assert batch.provider == "test"
    assert batch.bookmaker == "draftkings"
    assert batch.sport == "nba"
    assert batch.rows[0]["price_decimal"] == 1.91

    changes = store.changes_for_selection(batch.rows[0]["selection_key"])
    assert len(changes) == 2
    assert batch.row_ids == (changes[0]["id"],)
    assert changes[1]["id"] not in batch.row_ids


def test_archive_completion_requires_both_verified_drive_objects(tmp_path) -> None:
    store, now = _seed_closed_state(tmp_path / "rival.sqlite")
    queue = SQLiteArchiveQueue(store.path)
    batch = queue.next_batch(cutoff=now - timedelta(hours=1), max_rows=100)
    assert batch is not None

    queue.set_history_file(batch.batch_id, "history-file")

    try:
        queue.mark_complete(
            batch_id=batch.batch_id,
            row_ids=batch.row_ids,
            completed_at=now,
        )
    except RuntimeError as exc:
        assert "history and manifest" in str(exc)
    else:
        raise AssertionError("batch completed without a manifest")

    queue.set_manifest_file(batch.batch_id, "manifest-file")
    queue.mark_complete(
        batch_id=batch.batch_id,
        row_ids=batch.row_ids,
        completed_at=now,
    )

    record = queue.batch_record(batch.batch_id)
    assert record is not None
    assert record["status"] == "complete"

    with store.connect() as connection:
        rows = connection.execute(
            "SELECT id, archived_batch_id FROM odds_changes ORDER BY id"
        ).fetchall()
    assert rows[0]["archived_batch_id"] == batch.batch_id
    assert rows[1]["archived_batch_id"] is None
