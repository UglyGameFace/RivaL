"""Per-user free AI fairness is transactionally shared across RivaL workers."""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


@pytest.fixture(params=["sqlite", "postgres"])
def market(request, tmp_path):
    if request.param == "postgres":
        dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("Requires PostgreSQL CI database")
        store = PostgresHotStore(dsn)
    else:
        store = SQLiteHotStore(tmp_path / "fair.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    model = f"free-fair-{uuid.uuid4().hex}"
    try:
        yield market, model
    finally:
        with store.connect() as conn:
            conn.execute("DELETE FROM perp_ai_requester_quota WHERE model=?", (model,))
            conn.execute("DELETE FROM perp_ai_quota WHERE model=?", (model,))
        store.close()


def test_one_discord_user_cannot_monopolize_account_budget(market):
    engine, model = market
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    def attempt(offset, requester):
        return engine.reserve_ai_request(
            model=model, now=now + timedelta(seconds=offset * 60),
            daily_max=18, cooldown_seconds=60, requester_id=requester,
        )

    assert [attempt(i, 1234) for i in range(3)] == [True, True, True]
    assert attempt(3, 1234) is False
    one_user = engine.ai_request_status(
        model=model, now=now + timedelta(seconds=3 * 60),
        daily_max=18, cooldown_seconds=60, requester_id=1234,
    )
    assert "requester_budget_exhausted" in one_user["reasons"]
    assert one_user["requester_used_today"] == 3
    assert one_user["requester_daily_limit"] == 3
    assert one_user["requests_today"] == 3  # declined attempt rolled back
    assert attempt(3, 5678)
    other = engine.ai_request_status(
        model=model, now=now + timedelta(seconds=4 * 60),
        daily_max=18, cooldown_seconds=60, requester_id=5678,
    )
    assert other["requester_used_today"] == 1
    assert other["requests_today"] == 4
    digest = hashlib.sha256(b"discord:1234").hexdigest()
    with engine.store.connect() as conn:
        rows = conn.execute(
            "SELECT requester_sha256 FROM perp_ai_requester_quota WHERE model=?",
            (model,),
        ).fetchall()
    assert {r["requester_sha256"] for r in rows} == {
        digest, hashlib.sha256(b"discord:5678").hexdigest(),
    }
    # No plain Discord IDs or additional provider request in the quota ledger.


def test_requested_share_resets_next_day_without_disabling_shared_cap(market):
    engine, model = market
    now = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
    def reserve(at, who):
        return engine.reserve_ai_request(
            model=model, now=at, daily_max=2,
            cooldown_seconds=60, requester_id=who,
        )
    assert reserve(now, 42)
    assert reserve(now + timedelta(minutes=1), 43)
    assert not reserve(now + timedelta(minutes=2), 44)
    assert reserve(now + timedelta(days=1), 42)
    assert engine.ai_request_status(
        model=model, now=now + timedelta(days=1, minutes=1),
        daily_max=2, cooldown_seconds=60, requester_id=42,
    )["requester_used_today"] == 1


def test_reject_untrusted_requester_identity_without_charging(market):
    engine, model = market
    now = datetime(2026, 10, 30, 15, tzinfo=UTC)
    for invalid in (0, -1, True, "1234"):
        with pytest.raises(ValueError, match="invalid AI requester"):
            engine.reserve_ai_request(
                model=model, now=now, daily_max=18,
                cooldown_seconds=60, requester_id=invalid,
            )
    assert engine.ai_request_status(
        model=model, now=now, daily_max=18, cooldown_seconds=60,
    )["requests_today"] == 0
