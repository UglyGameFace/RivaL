"""Live sports discovery is based on observed provider data, not marketing claims."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.sports_discovery import (
    discovered_leagues,
    sports_discovery_embed,
)
from parlay_bot.storage.hot import SQLiteHotStore


def _sport(store, *, now, key, sport, tournament, future=True, fresh=True):
    start = now + timedelta(days=1) if future else now - timedelta(minutes=1)
    observed = now - timedelta(minutes=2 if fresh else 65)
    with store.connect() as con:
        con.execute(
            """INSERT INTO fixtures
               (provider,fixture_id,sport_id,tournament_id,sport_name,
                tournament_name,start_time,ingested_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                "sportsgameodds", key, sport, tournament, sport,
                tournament, start.isoformat(), observed.isoformat(),
            ),
        )
        con.execute(
            """INSERT INTO current_odds
               (selection_key,provider,fixture_id,bookmaker,market_id,outcome_id,
                player_id,active,main_line,price_decimal,
                changed_at,observed_at,state_fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, "sportsgameodds", key, "draftkings", "moneyline", "home",
                "", 1, 1, 2.15,
                observed.isoformat(), observed.isoformat(), "abc",
            ),
        )


def test_sports_are_discovered_by_real_live_fixtures_not_static_enum(tmp_path):
    store = SQLiteHotStore(tmp_path / "sport.sqlite")
    store.initialize()
    now = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
    try:
        _sport(store, now=now, key="soccer1", sport="Soccer", tournament="Premier League")
        _sport(store, now=now, key="cricket1", sport="Cricket", tournament="IPL")
        _sport(
            store, now=now, key="stale1", sport="Baseball",
            tournament="MLB", fresh=False,
        )
        _sport(
            store, now=now, key="past1", sport="Basketball",
            tournament="NBA", future=False,
        )
        leagues = discovered_leagues(store, now=now)
        assert {row["sport_name"] for row in leagues} == {"Soccer", "Cricket"}
        assert all(row["events"] == 1 for row in leagues)
        embed = sports_discovery_embed(
            store, configured_leagues=("EPL", "IPL"),
            collector_enabled=True, now=now,
        )
        assert "Premier League" in embed.fields[0].value
        assert "Cricket" in embed.fields[0].value
        assert "MLB" not in embed.fields[0].value
        assert "EPL" in embed.fields[1].value
    finally:
        store.close()


def test_disconnected_or_empty_sports_returns_no_fake_live_market(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    store.initialize()
    now = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
    try:
        no_provider = sports_discovery_embed(
            store, configured_leagues=(), collector_enabled=False, now=now,
        )
        assert "No live sportsbook connection" in no_provider.fields[0].name
        no_prices = sports_discovery_embed(
            store, configured_leagues=("NBA",), collector_enabled=True, now=now,
        )
        assert "No recent markets" in no_prices.fields[0].name
        assert discovered_leagues(
            store, now=now + timedelta(hours=5),
        ) == []
    finally:
        store.close()
