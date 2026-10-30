"""Auto-Hunt outbox query portability and per-tier delivery accounting.

These queries used SQL LIKE with a bare percent inside parameterized psycopg
statements, so production PostgreSQL raised ProgrammingError although SQLite
integration tests passed.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.auto_hunt import AutoHuntStore
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


@pytest.mark.parametrize("backend", ("sqlite", "postgres"))
def test_outbox_prefix_filters_are_portable_and_tiers_do_not_overlap(
    tmp_path, backend,
):
    if backend == "postgres":
        dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("RIVAL_TEST_DATABASE_URL not configured")
        hot = PostgresHotStore(dsn)
    else:
        hot = SQLiteHotStore(tmp_path / "hunt_prefix.sqlite")
    store = AutoHuntStore(hot)
    store.initialize()
    user_id = "test-rival-hunt-" + uuid.uuid4().hex
    now = datetime(2026, 10, 30, 18, 20, tzinfo=UTC)
    expires = now + timedelta(minutes=5)

    # All rows represent genuinely sent alerts on the same day. Each
    # subscription tier gets its own separate daily quota and exact prefix.
    cases = (
        ("qualified|window-one", "hyperliquid"),
        ("flip|window-two", "hyperliquid"),
        ("research-window", "hyperliquid"),
        ("qualifiedish|not-a-prefix", "hyperliquid"),
        ("polymarket-window", "polymarket"),
        ("live-shift-window", "live_trend"),
    )
    try:
        with hot.connect() as conn:
            for key, venue in cases:
                conn.execute(
                    """INSERT INTO rival_hunt_delivery
                       (user_id,forecast_key,asset,horizon,venue,
                        issued_at,expires_at,attempted_at,delivered_at,
                        message_id,state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        user_id, key, "BTC", 5, venue,
                        now.isoformat(), expires.isoformat(),
                        now.isoformat(), now.isoformat(), key, "sent",
                    ),
                )
        assert store.count_sent_today(user_id, "qualified", now) == 1
        assert store.count_sent_today(user_id, "flip", now) == 1
        assert store.count_sent_today(user_id, "polymarket", now) == 1
        assert store.count_sent_today(user_id, "live_shift", now) == 1
        assert store.count_sent_today(user_id, "research", now) == 2
        assert store.has_qualified_sent_window(user_id, "BTC", 5, expires)
        assert not store.has_qualified_sent_window(
            user_id, "BTC", 15, expires,
        )
        assert not store.has_qualified_sent_window(
            user_id, "ETH", 5, expires,
        )
    finally:
        with hot.connect() as conn:
            conn.execute(
                "DELETE FROM rival_hunt_delivery WHERE user_id=?", (user_id,),
            )
        hot.close()
