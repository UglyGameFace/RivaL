"""Historic provider accounting remains readable for provenance audits."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def test_ai_quota_status_explains_remaining_wait_and_daily_usage(tmp_path):
    store = SQLiteHotStore(tmp_path / "quota.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime(2026, 10, 28, 13, 25, tzinfo=UTC)
    try:
        initial = market.ai_request_status(
            model="gemini-3.8-flash", now=now,
            daily_max=24, cooldown_seconds=900,
        )
        assert initial["ready"]
        assert initial["requests_today"] == 0
        assert initial["retry_at"] is None
        assert market.reserve_ai_request(
            model="gemini-3.8-flash", now=now,
            daily_max=24, cooldown_seconds=900,
        )
        immediate = market.ai_request_status(
            model="gemini-3.8-flash", now=now + timedelta(minutes=1),
            daily_max=24, cooldown_seconds=900,
        )
        assert immediate["requests_today"] == 1
        assert immediate["reasons"] == ("minimum_spacing",)
        assert immediate["retry_at"] == now + timedelta(minutes=15)
        market.pause_ai_requests(
            model="gemini-3.8-flash", now=now, seconds=3600,
        )
        blocked = market.ai_request_status(
            model="gemini-3.8-flash", now=now + timedelta(minutes=13),
            daily_max=24, cooldown_seconds=900,
        )
        assert "provider_error_pause" in blocked["reasons"]
        assert "minimum_spacing" in blocked["reasons"]
        assert blocked["retry_at"] == now + timedelta(hours=1)
        permitted = market.ai_request_status(
            model="gemini-3.8-flash", now=now + timedelta(hours=1),
            daily_max=24, cooldown_seconds=900,
        )
        assert permitted["ready"]
        assert permitted["requests_today"] == 1
        assert permitted["retry_at"] is None
        # Status inspection never reserves a request.
        with store.connect() as conn:
            assert conn.execute(
                "SELECT used FROM perp_ai_quota WHERE model=?",
                ("gemini-3.8-flash",),
            ).fetchone()["used"] == 1
    finally:
        store.close()


def test_day_cap_is_shown_as_next_midnight_utc(tmp_path):
    store = SQLiteHotStore(tmp_path / "daily.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime(2026, 10, 28, 13, 25, tzinfo=UTC)
    try:
        assert market.reserve_ai_request(
            model="gemini-3.8-flash", now=now,
            daily_max=1, cooldown_seconds=900,
        )
        status = market.ai_request_status(
            model="gemini-3.8-flash",
            now=now + timedelta(minutes=20),
            daily_max=1, cooldown_seconds=900,
        )
        assert not status["ready"]
        assert status["reasons"] == ("daily_budget_exhausted",)
        assert status["retry_at"] == datetime(2026, 10, 29, tzinfo=UTC)
        tomorrow = market.ai_request_status(
            model="gemini-3.8-flash",
            now=datetime(2026, 10, 29, 0, 1, tzinfo=UTC),
            daily_max=1, cooldown_seconds=900,
        )
        assert tomorrow["ready"]
    finally:
        store.close()


def test_midnight_resets_daily_count_but_preserves_previous_day_spacing(tmp_path):
    store = SQLiteHotStore(tmp_path / "midnight-spacing.sqlite")
    market = PerpetualMarketStore(store)
    previous = datetime(2026, 10, 28, 23, 55, tzinfo=UTC)
    next_day = datetime(2026, 10, 29, 0, 1, tzinfo=UTC)
    try:
        assert market.reserve_ai_request(
            model="gemini-flash", now=previous, daily_max=1,
            cooldown_seconds=900,
        )
        # A new UTC date resets only the daily count, not 15-minute spacing.
        blocked = market.ai_request_status(
            model="gemini-flash", now=next_day, daily_max=1,
            cooldown_seconds=900,
        )
        assert blocked["requests_today"] == 0
        assert blocked["reasons"] == ("minimum_spacing",)
        assert blocked["retry_at"] == previous + timedelta(minutes=15)
        assert not market.reserve_ai_request(
            model="gemini-flash", now=next_day, daily_max=1,
            cooldown_seconds=900,
        )
        assert market.reserve_ai_request(
            model="gemini-flash", now=previous + timedelta(minutes=15),
            daily_max=1, cooldown_seconds=900,
        )
        assert not market.reserve_ai_request(
            model="gemini-flash", now=previous + timedelta(minutes=35),
            daily_max=1, cooldown_seconds=900,
        )
    finally:
        store.close()


def test_midnight_preserves_provider_429_pause_across_bot_restart(tmp_path):
    filename = tmp_path / "midnight-provider.sqlite"
    original = SQLiteHotStore(filename)
    market = PerpetualMarketStore(original)
    previous = datetime(2026, 10, 28, 23, 58, tzinfo=UTC)
    try:
        assert market.reserve_ai_request(
            model="gemini-flash", now=previous, daily_max=24,
            cooldown_seconds=900,
        )
        market.pause_ai_requests(
            model="gemini-flash", now=previous, seconds=3600,
        )
    finally:
        original.close()
    restarted = SQLiteHotStore(filename)
    other = PerpetualMarketStore(restarted)
    try:
        now = datetime(2026, 10, 29, 0, 25, tzinfo=UTC)
        blocked = other.ai_request_status(
            model="gemini-flash", now=now, daily_max=24,
            cooldown_seconds=900,
        )
        assert blocked["requests_today"] == 0
        assert blocked["reasons"] == ("provider_error_pause",)
        assert blocked["retry_at"] == previous + timedelta(hours=1)
        assert not other.reserve_ai_request(
            model="gemini-flash", now=now, daily_max=24,
            cooldown_seconds=900,
        )
        assert other.ai_request_status(
            model="gemini-flash", now=previous + timedelta(hours=1),
            daily_max=24, cooldown_seconds=900,
        )["ready"]
        assert other.reserve_ai_request(
            model="gemini-flash", now=previous + timedelta(hours=1),
            daily_max=24, cooldown_seconds=900,
        )
    finally:
        restarted.close()
