"""Provider-finalized, immutable sports scores and causal sport model tests."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.research.sports_results import (
    ResultIntegrityError,
    SportsResultStore,
    evaluate_sports_model,
    model_probabilities,
    normal_home_win,
    parse_final_score,
    soccer_home_draw_away,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def _event(*, event_id="sgo-final", sport="SOCCER", league="EPL",
           home_score=2, away_score=1, finalized=True, starts=None):
    start = starts or datetime.now(UTC) - timedelta(days=3)
    return {
        "eventID": event_id, "sportID": sport, "leagueID": league,
        "status": {
            "finalized": finalized, "cancelled": False,
            "startsAt": start.isoformat(),
        },
        "teams": {
            "home": {"teamID": "home-team"},
            "away": {"teamID": "away-team"},
        },
        "scores": {"home": home_score, "away": away_score},
    }


def test_finalized_scoring_requires_real_ids_finality_and_bounded_numbers():
    at = datetime.now(UTC)
    assert parse_final_score(_event(), observed_at=at).outcome == 1
    cases = [
        {"status": {"finalized": False}},
        {"scores": {"home": True, "away": 0}},
        {"scores": {"home": 90, "away": 0}},
        {"teams": {"home": {"names": {"long": "Someone"}}, "away": {}}},
        {"cancelled": True},
        {"sportID": "UNSUPPORTED"},
    ]
    for mutation in cases:
        event = _event()
        event.update(mutation)
        with pytest.raises(ResultIntegrityError):
            parse_final_score(event, observed_at=at)


def test_immutable_digested_sports_results_and_poll_budget(tmp_path):
    store = SQLiteHotStore(tmp_path / "sports.sqlite")
    research = SportsResultStore(store)
    at = datetime.now(UTC)
    try:
        assert research.save_finalized(_event(), observed_at=at) == "inserted"
        assert research.save_finalized(_event(), observed_at=at) == "unchanged"
        wrong = _event(home_score=0)
        with pytest.raises(ResultIntegrityError, match="contradict"):
            research.save_finalized(wrong, observed_at=at)
        persisted = research.all_results(sport="SOCCER", league="EPL")
        assert len(persisted) == 1 and persisted[0].home_score == 2
        assert research.evaluate(sport="SOCCER", league="EPL").status == "NOT_QUALIFIED"
        research.initialize()
        research.mark_checked(["sgo-final"], now=at)
        with store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_sport_results",
            ).fetchone()["n"]
            assert count == 1
    finally:
        store.close()


def test_sport_specific_distribution_is_normalized_and_not_claimed_profitable():
    h, d, a = soccer_home_draw_away(1.5, 1.0)
    assert abs(h + d + a - 1.0) < 1e-10
    assert h > a and 0 < d < 1
    p, draw = normal_home_win(5.0, 12.0)
    assert p > 0.5 and draw == 0
    assert model_probabilities(
        [], sport="BASKETBALL", league="NBA",
        home_id="a", away_id="b",
    ) is None
    assert evaluate_sports_model(
        [], sport="BASKETBALL", league="NBA",
    ).status == "NOT_QUALIFIED"


@pytest.mark.asyncio
async def test_official_result_lookup_is_bounded_verified_and_reuses_quota():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/account/usage":
            return httpx.Response(200, json={
                "success": True, "data": {
                    "tier": "amateur", "rateLimits": {},
                },
            })
        assert req.url.path == "/events"
        assert req.url.params["eventID"] == "a,b"
        return httpx.Response(200, json={"success": True, "data": [
            {"eventID": "a", "status": {"finalized": True}},
            {"eventID": "b", "status": {"finalized": False}},
        ]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SportsGameOddsClient("fake", client=http, request_cooldown_seconds=0)
    try:
        rows = await provider.get_result_events(("a", "b"))
        assert len(rows) == 2
        assert calls == ["/account/usage", "/events"]
        with pytest.raises(ValueError):
            await provider.get_result_events(("a,b",))
        with pytest.raises(ValueError):
            await provider.get_result_events(tuple(str(i) for i in range(9)))
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_result_lookup_rejects_wrong_events():
    def handler(req):
        if req.url.path == "/account/usage":
            return httpx.Response(200, json={"success": True, "data": {
                "rateLimits": {},
            }})
        return httpx.Response(200, json={"success": True, "data": [
            {"eventID": "not-requested"},
        ]})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SportsGameOddsClient("fake", client=http, request_cooldown_seconds=0)
    try:
        with pytest.raises(Exception, match="Unexpected event"):
            await provider.get_result_events(("requested",))
    finally:
        await http.aclose()


def test_postgres_sports_final_source_survives_restart():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL 16 test database configured")
    store = PostgresHotStore(dsn)
    ledger = SportsResultStore(store)
    at = datetime.now(UTC)
    event = _event(event_id="rival-sport-final-ci-927001")
    try:
        assert ledger.save_finalized(event, observed_at=at) in ("inserted", "unchanged")
        restarted = SportsResultStore(PostgresHotStore(dsn))
        rows = restarted.all_results(sport="SOCCER", league="EPL")
        assert any(x.event_id == event["eventID"] for x in rows)
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM rival_sport_results WHERE event_id=?",
                (event["eventID"],),
            )
        store.close()


def test_sports_forward_ledger_settles_only_pregame_and_keeps_result_identity(tmp_path):
    store = SQLiteHotStore(tmp_path / "prospective.sqlite")
    research = SportsResultStore(store)
    event = _event(event_id="match-1142")
    kickoff = datetime.fromisoformat(event["status"]["startsAt"])
    issued = kickoff - timedelta(hours=2)
    try:
        research.initialize()
        assert research.issue_forecast(
            {
                **event,
                "status": {
                    "started": False, "finalized": False,
                    "startsAt": kickoff.isoformat(),
                },
            }, observed_at=issued,
        ) == "not_qualified"
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_sport_forecasts
                   (event_id,sport,league,home_id,away_id,model_version,
                    issued_at,starts_at,probability_json,baseline_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "match-1142", "SOCCER", "EPL",
                    "home-team", "away-team",
                    "soccer-poisson-home-away-v1",
                    issued.isoformat(), kickoff.isoformat(),
                    json.dumps([0.6, 0.2, 0.2]),
                    json.dumps([0.4, 0.3, 0.3]),
                ),
            )
        assert research.save_finalized(
            event, observed_at=kickoff + timedelta(hours=3),
        ) == "inserted"
        with store.connect() as conn:
            settled = conn.execute(
                "SELECT label,model_brier,baseline_brier FROM "
                "rival_sport_forecasts WHERE event_id=?",
                ("match-1142",),
            ).fetchone()
            assert settled["label"] == 0
            assert settled["model_brier"] < settled["baseline_brier"]
        assert research.save_finalized(
            event, observed_at=kickoff + timedelta(hours=4),
        ) == "unchanged"
    finally:
        store.close()


def test_shadow_champion_promotes_only_prospective_scored_evidence_and_revokes(tmp_path):
    store = SQLiteHotStore(tmp_path / "governance.sqlite")
    research = SportsResultStore(store)
    research.initialize()
    at = datetime.now(UTC)
    try:
        empty = research.promotion_status(sport="SOCCER", league="EPL", now=at)
        assert empty["status"] == "NOT_QUALIFIED"
        with store.connect() as conn:
            for day in range(20):
                for i in range(5):
                    start = at - timedelta(days=20 - day, hours=i)
                    conn.execute(
                        """INSERT INTO rival_sport_forecasts
                           (event_id,sport,league,home_id,away_id,model_version,
                            issued_at,starts_at,probability_json,baseline_json,
                            settled_at,label,model_brier,baseline_brier)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"match-{day}-{i}", "SOCCER", "EPL",
                            "h", "a", "soccer-poisson-home-away-v1",
                            (start - timedelta(hours=2)).isoformat(),
                            start.isoformat(),
                            json.dumps([0.55, 0.2, 0.25]),
                            json.dumps([0.4, 0.3, 0.3]),
                            (start + timedelta(hours=2)).isoformat(),
                            0, 0.20, 0.30,
                        ),
                    )
        qualified = research.promotion_status(sport="SOCCER", league="EPL", now=at)
        assert qualified["status"] == "SHADOW_CHAMPION"
        assert qualified["settled_unique_events"] == 100
        assert qualified["production_inference_changed"] is False
        assert qualified["live_trading_approved"] is False
        with store.connect() as conn:
            conn.execute(
                """UPDATE rival_sport_forecasts SET model_brier=0.5,
                   baseline_brier=0.3 WHERE sport='SOCCER' AND league='EPL'"""
            )
        revoked = research.promotion_status(
            sport="SOCCER", league="EPL", now=at + timedelta(days=1),
        )
        assert revoked["status"] == "REVOKED"
    finally:
        store.close()
