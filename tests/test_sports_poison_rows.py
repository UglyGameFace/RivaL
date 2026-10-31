"""A rejected forecast must not discard final evidence or monopolize repair."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

from parlay_bot.research.database_integrity import (
    sports_settlement_backlog,
    sports_settlement_exclusions,
)
from parlay_bot.research.sports_results import ResultIntegrityError, SportsResultStore
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore
from tests.test_sports_settlement_recovery import KICKOFF, event, insert_pending_forecast


@pytest.fixture(params=["sqlite", "postgres"])
def ledger(request, tmp_path):
    if request.param == "postgres":
        dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("No PostgreSQL test database configured")
        store = PostgresHotStore(dsn)
    else:
        store = SQLiteHotStore(tmp_path / "poison.sqlite")
    research = SportsResultStore(store)
    research.initialize()
    ids = []

    def new_id():
        value = "poison-test-" + uuid.uuid4().hex
        ids.append(value)
        return value

    yield store, research, new_id
    with store.connect() as conn:
        for value in ids:
            for table in ("rival_sport_settlement_exclusions", "rival_sport_forecasts",
                          "rival_sport_results"):
                conn.execute(f"DELETE FROM {table} WHERE event_id=?", (value,))
    store.close()


@pytest.mark.parametrize(("column", "value", "reason"), [
    ("home_id", "other-team", "identity_mismatch"),
    ("issued_at", KICKOFF.isoformat(), "not_pregame"),
    ("issued_at", "broken", "invalid_forecast_clock"),
    ("starts_at", (KICKOFF + timedelta(hours=1)).isoformat(), "kickoff_mismatch"),
    ("probability_json", "not json", "invalid_probability_vector"),
    ("probability_json", "null", "invalid_probability_vector"),
    ("probability_json", "[NaN,0.2,0.8]", "invalid_probability_vector"),
    ("probability_json", "[true,0,0]", "invalid_probability_vector"),
    ("baseline_json", "[0.2,0.2,0.2]", "invalid_probability_vector"),
])
def test_valid_final_result_survives_poison_forecast(ledger, column, value, reason):
    store, research, new_id = ledger
    key = new_id()
    insert_pending_forecast(store, event_id=key)
    with store.connect() as conn:
        conn.execute(f"UPDATE rival_sport_forecasts SET {column}=? WHERE event_id=?",
                     (value, key))
    result = event(event_id=key)
    assert research.save_finalized(result, observed_at=KICKOFF + timedelta(hours=2)) == "inserted"
    with store.connect() as conn:
        saved = conn.execute("SELECT * FROM rival_sport_results WHERE event_id=?", (key,)).fetchone()
        excluded = conn.execute(
            "SELECT * FROM rival_sport_settlement_exclusions WHERE event_id=?", (key,),
        ).fetchone()
        forecast = conn.execute(
            "SELECT * FROM rival_sport_forecasts WHERE event_id=?", (key,),
        ).fetchone()
    assert saved is not None
    assert excluded["reason"] == reason
    assert forecast["settled_at"] is None and forecast["model_brier"] is None
    restarted = SportsResultStore(store)
    assert restarted.save_finalized(result, observed_at=KICKOFF + timedelta(hours=3)) == "unchanged"
    assert restarted.reconcile_unsettled() == []
    assert sports_settlement_backlog(store) == 0
    assert sports_settlement_exclusions(store)[reason] >= 1
    with pytest.raises(ResultIntegrityError, match="contradict"):
        restarted.save_finalized(event(event_id=key, home=3),
                                 observed_at=KICKOFF + timedelta(hours=3))


def test_full_poison_batch_does_not_starve_next_forecast(ledger):
    store, research, new_id = ledger
    keys = [new_id() for _ in range(102)]
    # Simulate legacy rows: results exist before the pending forecasts.
    for i, key in enumerate(keys):
        research.save_finalized(event(event_id=key),
                                observed_at=KICKOFF + timedelta(hours=2, seconds=i))
        insert_pending_forecast(store, event_id=key)
    with store.connect() as conn:
        for key in keys[:-1]:
            conn.execute("UPDATE rival_sport_forecasts SET home_id='wrong' WHERE event_id=?",
                         (key,))
    assert sports_settlement_backlog(store) == 102
    assert research.reconcile_unsettled(limit=100) == []
    assert sports_settlement_backlog(store) == 2
    assert SportsResultStore(store).reconcile_unsettled(limit=100) == [("SOCCER", "EPL")]
    assert sports_settlement_backlog(store) == 0
    with store.connect() as conn:
        good = conn.execute("SELECT * FROM rival_sport_forecasts WHERE event_id=?",
                            (keys[-1],)).fetchone()
    assert good["settled_at"] is not None and good["label"] == 0


def test_basketball_tie_is_terminal_without_a_fake_loss(ledger):
    store, research, new_id = ledger
    key = new_id()
    insert_pending_forecast(store, event_id=key)
    with store.connect() as conn:
        conn.execute("UPDATE rival_sport_forecasts SET sport='BASKETBALL' WHERE event_id=?",
                     (key,))
    result = event(event_id=key, home=90, away=90)
    result["sportID"] = "BASKETBALL"
    research.save_finalized(result, observed_at=KICKOFF + timedelta(hours=2))
    assert sports_settlement_exclusions(store)["unsupported_basketball_tie"] >= 1
    assert sports_settlement_backlog(store) == 0
    assert research.reconcile_unsettled() == []
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM rival_sport_forecasts WHERE event_id=?", (key,)).fetchone()
    assert all(row[name] is None for name in ("settled_at", "label", "model_brier", "baseline_brier"))


def test_malformed_legacy_result_does_not_abort_batch(ledger):
    store, research, new_id = ledger
    for bad in (True, False):
        key = new_id()
        research.save_finalized(event(event_id=key), observed_at=KICKOFF + timedelta(hours=2))
        insert_pending_forecast(store, event_id=key)
        if bad:
            with store.connect() as conn:
                conn.execute("UPDATE rival_sport_results SET started_at='invalid' WHERE event_id=?",
                             (key,))
    assert research.reconcile_unsettled() == [("SOCCER", "EPL")]
    assert sports_settlement_exclusions(store)["invalid_result_record"] >= 1
    assert sports_settlement_backlog(store) == 0


def test_infrastructure_error_remains_retryable(ledger, monkeypatch):
    store, research, new_id = ledger
    key = new_id()
    insert_pending_forecast(store, event_id=key)
    original = research.settle_forecast

    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(research, "settle_forecast", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        research.save_finalized(event(event_id=key), observed_at=KICKOFF + timedelta(hours=2))
    with store.connect() as conn:
        assert conn.execute("SELECT 1 FROM rival_sport_results WHERE event_id=?",
                            (key,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM rival_sport_settlement_exclusions WHERE event_id=?",
                            (key,)).fetchone() is None
    monkeypatch.setattr(research, "settle_forecast", original)
    assert research.save_finalized(event(event_id=key),
                                   observed_at=KICKOFF + timedelta(hours=3)) == "inserted"
