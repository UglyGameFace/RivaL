"""Sports settlement survives a crash between insert and settle.

The old save_finalized returned "unchanged" on an identical redelivery
without retrying settlement, so a forecast could stay pending forever
after a crash. The fix settles in the same transaction as the insert
and retries settlement on redelivery; a bounded reconciliation worker
covers results the provider never redelivers.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.sports_results import (
    MODELS,
    ResultIntegrityError,
    SportsResultStore,
)
from parlay_bot.storage.hot import SQLiteHotStore

KICKOFF = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def event(*, event_id="sgo-recover-1", home=2, away=1):
    return {
        "eventID": event_id, "sportID": "SOCCER", "leagueID": "EPL",
        "status": {
            "finalized": True, "cancelled": False,
            "startsAt": KICKOFF.isoformat(),
        },
        "teams": {
            "home": {"teamID": "home-team"},
            "away": {"teamID": "away-team"},
        },
        "scores": {"home": home, "away": away},
        "results": {
            "reg": {
                "home": {"points": home},
                "away": {"points": away},
            },
        },
    }


def insert_pending_forecast(store, *, event_id="sgo-recover-1"):
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_sport_forecasts
               (event_id,sport,league,home_id,away_id,model_version,
                issued_at,starts_at,probability_json,baseline_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, "SOCCER", "EPL", "home-team", "away-team",
                MODELS["SOCCER"], (KICKOFF - timedelta(hours=2)).isoformat(),
                KICKOFF.isoformat(), "[0.6,0.25,0.15]", "[0.4,0.3,0.3]",
            ),
        )


def forecast_row(store, *, event_id="sgo-recover-1"):
    with store.connect() as conn:
        return conn.execute(
            "SELECT settled_at,label FROM rival_sport_forecasts WHERE event_id=?",
            (event_id,),
        ).fetchone()


def test_redelivery_settles_crash_stranded_forecast(tmp_path):
    store = SQLiteHotStore(tmp_path / "recover.sqlite")
    research = SportsResultStore(store)
    try:
        assert research.save_finalized(
            event(), observed_at=KICKOFF + timedelta(hours=2),
        ) == "inserted"
        assert forecast_row(store) is None
        insert_pending_forecast(store)
        assert forecast_row(store)["settled_at"] is None
        assert research.save_finalized(
            event(), observed_at=KICKOFF + timedelta(hours=3),
        ) == "unchanged"
        row = forecast_row(store)
        assert row["settled_at"] is not None
        assert int(row["label"]) == 0
    finally:
        store.close()


def test_reconciliation_settles_without_redelivery(tmp_path):
    store = SQLiteHotStore(tmp_path / "reconcile.sqlite")
    research = SportsResultStore(store)
    try:
        assert research.save_finalized(
            event(), observed_at=KICKOFF + timedelta(hours=2),
        ) == "inserted"
        insert_pending_forecast(store)
        assert research.reconcile_unsettled() == [("SOCCER", "EPL")]
        assert forecast_row(store)["settled_at"] is not None
        assert research.reconcile_unsettled() == []
    finally:
        store.close()


def test_contradicting_redelivery_still_raises(tmp_path):
    store = SQLiteHotStore(tmp_path / "conflict.sqlite")
    research = SportsResultStore(store)
    try:
        assert research.save_finalized(
            event(), observed_at=KICKOFF + timedelta(hours=2),
        ) == "inserted"
        with pytest.raises(ResultIntegrityError):
            research.save_finalized(
                event(home=3), observed_at=KICKOFF + timedelta(hours=3),
            )
    finally:
        store.close()
