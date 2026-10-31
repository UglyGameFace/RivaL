"""Cross-process account budget safety for both sports provider integrations."""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.oddspapi import OddsPapiClient
from parlay_bot.providers.owner_limits import OwnerLimit, OwnerLimitStore
from parlay_bot.providers.shared_budget import UNKNOWN_PERIOD, QuotaClaim, SharedProviderBudget
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def _sgo_usage(*, month=100, end=None, minute=1, minute_end=None):
    month_data = {"max-entities": 2500, "current-entities": month}
    minute_data = {"max-requests": 10, "current-requests": minute}
    if end is not None:
        month_data["currentIntervalEndTime"] = end
    if minute_end is not None:
        minute_data["currentIntervalEndTime"] = minute_end
    return {"success": True, "data": {"tier": "paid", "rateLimits": {
        "per-month": month_data, "per-minute": minute_data,
    }}}


def _account():
    return {"current_subscription_id": "sub-1", "subscriptions": [{
        "subscription_id": "sub-1", "is_active": True,
        "request_limit": 101, "request_count": 100,
        "sport_ids": [4], "bookmakers": {},
    }]}


def _sgo_handler(sent, *, end=None, minute=1, minute_end=None):
    def handler(request):
        if request.url.path == "/v2/account/usage":
            return httpx.Response(200, json=_sgo_usage(
                end=end, minute=minute, minute_end=minute_end,
            ))
        sent.append(request.url.path)
        return httpx.Response(200, json={"success": True, "data": []})
    return handler


@pytest.mark.asyncio
async def test_two_sports_clients_on_different_db_store_instances_share_owner_cap(tmp_path):
    path = str(tmp_path / "rival.sqlite")
    a, b = SQLiteHotStore(path), SQLiteHotStore(path)
    OwnerLimitStore(a).save(OwnerLimit("sportsgameodds", "paid", 103, 0))
    sent = []
    try:
        async with (
            httpx.AsyncClient(
                base_url="https://api.sportsgameodds.com/v2",
                transport=httpx.MockTransport(_sgo_handler(sent)),
            ) as client_a,
            httpx.AsyncClient(
                base_url="https://api.sportsgameodds.com/v2",
                transport=httpx.MockTransport(_sgo_handler(sent)),
            ) as client_b,
        ):
            p1 = SportsGameOddsClient(
                "same-key", client=client_a, owner_limits=OwnerLimitStore(a),
                monthly_entity_reserve=0, request_cooldown_seconds=0,
            )
            p2 = SportsGameOddsClient(
                "same-key", client=client_b, owner_limits=OwnerLimitStore(b),
                monthly_entity_reserve=0, request_cooldown_seconds=0,
            )
            out = await asyncio.gather(
                p1.get_events(league_ids=["NBA"], limit=2),
                p2.get_events(league_ids=["NBA"], limit=2),
            )
            assert len(out) == 2
            assert sent == ["/v2/events", "/v2/events"]
            row = SharedProviderBudget(
                a, provider="sportsgameodds", api_key="same-key",
            )
            with pytest.raises(QuotaReserveReached, match="Shared"):
                await asyncio.to_thread(
                    row.reserve, QuotaClaim(
                        "monthly_entities", UNKNOWN_PERIOD, 100, 2500, 0, 1,
                        owner_cap=True,
                    ),
                )
    finally:
        a.close()
        b.close()


@pytest.mark.asyncio
async def test_sgo_minute_quota_and_monthly_reservation_are_one_transaction(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "minute.sqlite"))
    end = datetime(2026, 10, 31, 14, 1, tzinfo=UTC).isoformat()
    calls = []
    try:
        async with httpx.AsyncClient(
            base_url="https://api.sportsgameodds.com/v2",
            transport=httpx.MockTransport(_sgo_handler(
                calls, minute=9, minute_end=end,
            )),
        ) as http_client:
            p = SportsGameOddsClient(
                "minute-key", client=http_client, owner_limits=OwnerLimitStore(store),
                monthly_entity_reserve=0, request_cooldown_seconds=0,
            )
            await p.get_events(league_ids=["NBA"], limit=1)
            with pytest.raises(QuotaReserveReached, match="minute_requests"):
                await p.get_events(league_ids=["NBA"], limit=1)
        assert calls == ["/v2/events"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_two_oddspapi_clients_do_not_double_spend_same_account(tmp_path):
    path = str(tmp_path / "odds.sqlite")
    a, b = SQLiteHotStore(path), SQLiteHotStore(path)
    billable = []
    def handler(req):
        if req.url.path == "/v4/account":
            return httpx.Response(200, json=_account())
        billable.append(req.url.path)
        return httpx.Response(503)
    try:
        async with (
            httpx.AsyncClient(
                base_url="https://api.oddspapi.io",
                transport=httpx.MockTransport(handler),
            ) as client_a,
            httpx.AsyncClient(
                base_url="https://api.oddspapi.io",
                transport=httpx.MockTransport(handler),
            ) as client_b,
        ):
            clients = [
                OddsPapiClient(
                    "odds-shared", client=http_client, owner_limits=OwnerLimitStore(store),
                    quota_reserve=0, cooldowns={"account": 0, "odds": 0},
                ) for http_client, store in ((client_a, a), (client_b, b))
            ]
            outcomes = await asyncio.gather(
                *(client.get_odds("fx") for client in clients),
                return_exceptions=True,
            )
            assert sum(isinstance(x, httpx.HTTPStatusError) for x in outcomes) == 1
            assert sum(isinstance(x, QuotaReserveReached) for x in outcomes) == 1
            assert billable == ["/v4/odds"]
    finally:
        a.close()
        b.close()


@pytest.mark.asyncio
async def test_shared_monthly_period_only_resets_on_strictly_newer_provider_window(tmp_path):
    a = SQLiteHotStore(str(tmp_path / "window.sqlite"))
    budget = SharedProviderBudget(a, provider="sportsgameodds", api_key="window")
    prior = datetime(2026, 11, 1, tzinfo=UTC).isoformat()
    later = (datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=30)).isoformat()
    try:
        claim = lambda period: QuotaClaim("monthly_entities", period, 100, 101, 0, 1)
        assert budget.reserve(claim(prior)) == (1,)
        with pytest.raises(QuotaReserveReached):
            budget.reserve(claim(prior))
        with pytest.raises(QuotaReserveReached, match="older"):
            budget.reserve(claim(UNKNOWN_PERIOD))
        assert budget.reserve(claim(later)) == (1,)
        with pytest.raises(QuotaReserveReached, match="older"):
            budget.reserve(claim(prior))
    finally:
        a.close()


def test_postgres_shared_quota_is_atomic_across_independent_connections():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("RIVAL_TEST_DATABASE_URL is required")
    from uuid import uuid4

    a, b = PostgresHotStore(dsn), PostgresHotStore(dsn)
    key = "ci-" + str(uuid4())
    x = SharedProviderBudget(a, provider="sportsgameodds", api_key=key)
    y = SharedProviderBudget(b, provider="sportsgameodds", api_key=key)
    claim = QuotaClaim("monthly_entities", UNKNOWN_PERIOD, 100, 101, 0, 1)
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda budget: _try_reserve(budget, claim), [x, y],
            ))
        assert sorted(results) == ["blocked", "reserved"]
    finally:
        with a.connect() as conn:
            conn.execute(
                "DELETE FROM rival_provider_quota_reservations WHERE account_key=?",
                (x.account_key,),
            )
        a.close()
        b.close()


def _try_reserve(budget, claim):
    try:
        budget.reserve(claim)
    except QuotaReserveReached:
        return "blocked"
    return "reserved"
