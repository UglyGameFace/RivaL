"""Sports issuance records the actual insertion time, before kickoff.

The old code inserted the pre-computation observation timestamp as
issued_at without rechecking the clock, so a computation that crossed
kickoff could still look pregame. The fix records the insertion time
and rejects anything that crosses kickoff. A qualified league fixture
(140 strength-based soccer games) proves the path end to end.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.research.sports_results import SportsResultStore
from parlay_bot.storage.hot import SQLiteHotStore

STRENGTH = {"A": 2.0, "B": 1.0, "C": 0.0, "D": 0.5}
PAIRS = [("A", "B"), ("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"), ("C", "D")]
BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
KICKOFF = BASE + timedelta(days=140, hours=0)


def seed_qualified_league(store):
    with store.connect() as conn:
        for day in range(140):
            first, second = PAIRS[day % 6]
            home, away = (first, second) if day % 2 == 0 else (second, first)
            home_strength, away_strength = STRENGTH[home], STRENGTH[away]
            home_goals = max(0, int(2 + (home_strength - away_strength)))
            away_goals = max(0, int(1 + (away_strength - home_strength)))
            started = BASE + timedelta(days=day)
            conn.execute(
                """INSERT INTO rival_sport_results
                   (event_id,provider,sport,league,home_id,away_id,
                    home_score,away_score,started_at,first_observed_at,digest)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"g{day}", "sportsgameodds", "SOCCER", "EPL", home, away,
                    home_goals, away_goals, started.isoformat(),
                    (started + timedelta(hours=2)).isoformat(), f"d{day}",
                ),
            )


def pregame_event(*, event_id, kickoff):
    return {
        "eventID": event_id, "sportID": "SOCCER", "leagueID": "EPL",
        "status": {
            "started": False, "finalized": False, "cancelled": False,
            "startsAt": kickoff.isoformat(),
        },
        "teams": {
            "home": {"teamID": "A"},
            "away": {"teamID": "B"},
        },
    }


def forecast_row(store, *, event_id):
    with store.connect() as conn:
        return conn.execute(
            "SELECT issued_at,starts_at FROM rival_sport_forecasts WHERE event_id=?",
            (event_id,),
        ).fetchone()


def test_issuance_records_insertion_time_not_observation_time(tmp_path):
    store = SQLiteHotStore(tmp_path / "issued.sqlite")
    research = SportsResultStore(store)
    try:
        research.initialize()
        seed_qualified_league(store)
        observed = KICKOFF - timedelta(hours=12)
        inserted = KICKOFF - timedelta(hours=11, minutes=55)
        assert research.issue_forecast(
            pregame_event(event_id="new-1", kickoff=KICKOFF),
            observed_at=observed, now=inserted,
        ) == "issued"
        row = forecast_row(store, event_id="new-1")
        assert row["issued_at"] == inserted.isoformat()
        assert row["starts_at"] == KICKOFF.isoformat()
    finally:
        store.close()


def test_computation_crossing_kickoff_is_rejected(tmp_path):
    store = SQLiteHotStore(tmp_path / "crossed.sqlite")
    research = SportsResultStore(store)
    try:
        research.initialize()
        seed_qualified_league(store)
        assert research.issue_forecast(
            pregame_event(event_id="new-2", kickoff=KICKOFF),
            observed_at=KICKOFF - timedelta(minutes=10),
            now=KICKOFF + timedelta(minutes=1),
        ) == "crossed_kickoff"
        assert forecast_row(store, event_id="new-2") is None
    finally:
        store.close()
