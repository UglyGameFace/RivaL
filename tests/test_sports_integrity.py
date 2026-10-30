"""Sports integrity: unknown-start fixtures and self-included consensus.

A fixture with no start time must never enter a pregame parlay: a missing
timestamp does not prove the event has not started. And a candidate book's
edge must be measured against books OTHER than itself, so the benchmark
labeled "reference books" is actually independent of the price it judges.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.ingestion.models import NormalizedOddsBoard
from parlay_bot.parlays.math import no_vig_probabilities
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from tests.test_parlays import build_engine, observation, seed_fixture


def seed_unknown_start_fixture(store, *, number, dk_home, dk_away,
                               fd_home, fd_away):
    now = datetime.now(UTC) + timedelta(minutes=number)
    store.ingest_board(
        NormalizedOddsBoard(
            provider="test",
            fixture_id=f"fixture-{number}",
            sport_id=11,
            tournament_id=132,
            status_id=0,
            status_name="Pre-Game",
            start_time=None,
            participant1_name=f"Home {number}",
            participant2_name=f"Away {number}",
            sport_name="Basketball",
            tournament_name="NBA",
            observations=[
                observation(fixture=f"fixture-{number}", bookmaker="draftkings",
                            outcome="101", price=dk_home, changed=now),
                observation(fixture=f"fixture-{number}", bookmaker="draftkings",
                            outcome="102", price=dk_away, changed=now),
                observation(fixture=f"fixture-{number}", bookmaker="fanduel",
                            outcome="101", price=fd_home, changed=now),
                observation(fixture=f"fixture-{number}", bookmaker="fanduel",
                            outcome="102", price=fd_away, changed=now),
            ],
        )
    )


def test_unknown_start_fixture_never_enters_a_parlay(tmp_path):
    engine, store = build_engine(tmp_path)
    # The unknown-start fixture has the best odds on the board; if the
    # NULL-start loophole were still open, the slip would pick it.
    seed_unknown_start_fixture(
        store, number=1, dk_home=2.50, dk_away=1.60,
        fd_home=2.40, fd_away=1.65,
    )
    seed_fixture(
        store, number=2, dk_home=1.95, dk_away=1.95,
        fd_home=2.10, fd_away=1.80,
    )
    slip = engine.build_for_state(
        state_code="CT", leg_count=1, risk=RiskMode.BALANCED,
    )
    assert all(leg.fixture_id == "fixture-2" for leg in slip.legs)


def test_unknown_start_only_fixture_builds_nothing(tmp_path):
    engine, store = build_engine(tmp_path)
    seed_unknown_start_fixture(
        store, number=1, dk_home=2.50, dk_away=1.60,
        fd_home=2.40, fd_away=1.65,
    )
    with pytest.raises(ParlayBuildError):
        engine.build_for_state(
            state_code="CT", leg_count=1, risk=RiskMode.BALANCED,
        )


def test_candidate_edge_uses_only_external_books(tmp_path):
    engine, store = build_engine(tmp_path)
    seed_fixture(
        store, number=1, dk_home=2.10, dk_away=1.80,
        fd_home=1.95, fd_away=1.95,
    )
    candidates = engine._candidates(
        eligible_books={"draftkings", "fanduel"}, risk=RiskMode.BALANCED,
    )
    dk_home = next(
        c for c in candidates
        if c.bookmaker == "draftkings" and c.outcome_id == "101"
    )
    # FanDuel's no-vig home probability is the whole benchmark; DraftKings'
    # own 2.10 price must not pull its own fair toward itself.
    expected_fair = no_vig_probabilities([1.95, 1.95])[0]
    assert dk_home.fair_probability == pytest.approx(expected_fair)
    assert dk_home.reference_books == 1
    assert dk_home.price_edge == pytest.approx(2.10 * expected_fair - 1)
