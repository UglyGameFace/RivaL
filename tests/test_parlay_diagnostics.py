"""Builder diagnostics: a PASS must name the gate that eliminated everything."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.ingestion.models import NormalizedOddsBoard
from parlay_bot.parlays.models import ParlayBuildError
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_parlays import build_engine, observation, seed_fixture


def seed_single_book_fixture(store: SQLiteHotStore, *, number: int) -> None:
    """One fixture priced by a single book: no cross-book reference exists."""
    now = datetime.now(UTC) + timedelta(minutes=number)
    store.ingest_board(
        NormalizedOddsBoard(
            provider="test",
            fixture_id=f"solo-{number}",
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
                    fixture=f"solo-{number}",
                    bookmaker="draftkings",
                    outcome="101",
                    price=1.91,
                    changed=now,
                ),
                observation(
                    fixture=f"solo-{number}",
                    bookmaker="draftkings",
                    outcome="102",
                    price=1.91,
                    changed=now,
                ),
            ],
        )
    )


def test_no_rows_reports_empty_diagnostics(tmp_path) -> None:
    engine, _store = build_engine(tmp_path)
    with pytest.raises(ParlayBuildError):
        engine.build_for_state(state_code="CT", leg_count=1)
    assert engine.last_diagnostics["rows_seen"] == 0


def test_single_book_rows_count_insufficient_reference_books(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    seed_single_book_fixture(store, number=1)
    with pytest.raises(ParlayBuildError):
        engine.build_for_state(state_code="CT", leg_count=1)
    diagnostics = engine.last_diagnostics
    assert diagnostics["rows_seen"] == 2
    assert diagnostics["insufficient_reference_books"] == 2
    assert diagnostics["negative_edge"] == 0


def test_even_prices_count_below_advertised_edge(tmp_path) -> None:
    engine, store = build_engine(tmp_path)
    # Asymmetric books so the price vectors are not duplicates: dk implies
    # 50/50, fd implies roughly 51/49. Two rows land in [-2.5%, +2%) edge
    # (below the advertised screen, not negative) and two land negative.
    seed_fixture(
        store,
        number=1,
        dk_home=1.95,
        dk_away=1.95,
        fd_home=1.90,
        fd_away=2.00,
    )
    with pytest.raises(ParlayBuildError):
        engine.build_for_state(state_code="CT", leg_count=1)
    diagnostics = engine.last_diagnostics
    assert diagnostics["below_advertised_edge"] == 2
    assert diagnostics["negative_edge"] == 2
    assert diagnostics["duplicate_vectors"] == 0


def test_successful_build_still_records_rows_seen(tmp_path) -> None:
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
    slip = engine.build_for_state(state_code="CT", leg_count=3)
    assert len(slip.legs) == 3
    assert engine.last_diagnostics["rows_seen"] > 0
