"""Sports settlement survives a crash between insert and settle.

The old save_finalized returned "unchanged" on an identical redelivery
without retrying settlement, so a forecast could stay pending forever
after a crash. The fix settles in the same transaction as the insert
and retries settlement on redelivery; a bounded reconciliation worker
covers results the provider never redelivers.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.research.sports_results import (
    MODELS,
    ResultIntegrityError,
    SportsResultStore,
    parse_final_score,
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


def test_active_transaction_settlement_does_not_reinitialize(tmp_path, monkeypatch):
    """An existing transaction must not open a nested DDL connection."""
    store = SQLiteHotStore(tmp_path / "active-transaction.sqlite")
    research = SportsResultStore(store)
    try:
        research.initialize()
        insert_pending_forecast(store)
        score = parse_final_score(
            event(), observed_at=KICKOFF + timedelta(hours=2),
        )

        def forbidden_initialize():
            pytest.fail("Reinitialized schema inside an active transaction")

        monkeypatch.setattr(research, "initialize", forbidden_initialize)
        with store.connect() as conn:
            assert research.settle_forecast(score, conn=conn)
        assert forecast_row(store)["settled_at"] is not None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_background_reconciliation_runs_with_no_due_fixtures(tmp_path, monkeypatch):
    """An old result must settle even if no external result poll is due."""
    store = SQLiteHotStore(tmp_path / "recovery-loop.sqlite")
    store.initialize()
    research = SportsResultStore(store)
    try:
        research.save_finalized(
            event(), observed_at=KICKOFF + timedelta(hours=2),
        )
        insert_pending_forecast(store)

        class OneCycleBot:
            sports_results = research
            current_provider = object()
            closed = False

            async def wait_until_ready(self):
                return None

            def is_closed(self):
                return self.closed

        bot = OneCycleBot()
        bot.store = store
        original_sleep = asyncio.sleep

        async def finish_cycle(delay):
            if delay == 21600:
                bot.closed = True
            await original_sleep(0)

        monkeypatch.setattr("parlay_bot.discord_app.bot.asyncio.sleep", finish_cycle)
        assert research.due_fixtures(
            now=KICKOFF + timedelta(days=3),
        ) == []
        await RivalDiscordClient._sports_results_loop(bot)
        assert forecast_row(store)["settled_at"] is not None
        assert int(forecast_row(store)["label"]) == 0
    finally:
        store.close()
