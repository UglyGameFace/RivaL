"""Sports billable-event gate: avoid stale counters and unverified quotas."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.owner_limits import OwnerLimit, OwnerLimitStore
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.storage.hot import SQLiteHotStore


def usage(*, current=100, maximum=2500):
    return {"success": True, "data": {
        "tier": "paid",
        "rateLimits": {
            "per-month": {
                "max-entities": maximum,
                "current-entities": current,
            },
            "per-minute": {
                "max-requests": 50, "current-requests": 1,
            },
        },
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    usage(current=None),
    usage(maximum=None),
    usage(current="unlimited"),
    usage(current=-1),
    usage(maximum=-1),
    usage(current=2501),
    {"success": True, "data": {"rateLimits": {}}},
])
async def test_event_calls_fail_closed_without_authoritative_monthly_usage(payload):
    paths = []
    def handler(req):
        paths.append(req.url.path)
        assert req.url.path == "/v2/account/usage"
        return httpx.Response(200, json=payload)
    async with httpx.AsyncClient(
        base_url="https://api.sportsgameodds.com/v2",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = SportsGameOddsClient(
            "test", client=client, request_cooldown_seconds=0,
            monthly_entity_reserve=0,
        )
        with pytest.raises(QuotaReserveReached, match="unknown"):
            await provider.get_events(league_ids=["NBA"], limit=3)
        with pytest.raises(QuotaReserveReached, match="unknown"):
            await provider.get_result_events(("event-1",))
    assert paths == ["/v2/account/usage", "/v2/account/usage"]


@pytest.mark.asyncio
async def test_concurrent_provider_requests_respect_owner_cap_with_stale_usage(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "quota.sqlite"))
    limits = OwnerLimitStore(store)
    limits.save(OwnerLimit(
        provider="sportsgameodds", plan="paid",
        maximum_units=104, reserve_units=0,
    ))
    sent = []
    async def handler(req):
        if req.url.path == "/v2/account/usage":
            return httpx.Response(200, json=usage(current=100))
        sent.append(int(req.url.params["limit"]))
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"success": True, "data": []})
    try:
        async with httpx.AsyncClient(
            base_url="https://api.sportsgameodds.com/v2",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = SportsGameOddsClient(
                "test", client=client, owner_limits=limits,
                request_cooldown_seconds=0, monthly_entity_reserve=0,
            )
            outcomes = await asyncio.gather(*(
                provider.get_events(league_ids=["NBA"], limit=3)
                for _ in range(3)
            ), return_exceptions=True)
            assert sent == [3, 1]
            assert sum(isinstance(x, QuotaReserveReached) for x in outcomes) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_response_keeps_pessimistic_entity_reservation(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "quota.sqlite"))
    limits = OwnerLimitStore(store)
    limits.save(OwnerLimit(
        provider="sportsgameodds", plan="paid",
        maximum_units=101, reserve_units=0,
    ))
    events = []
    def handler(req):
        if req.url.path == "/v2/account/usage":
            return httpx.Response(200, json=usage(current=100))
        events.append(req.url.path)
        return httpx.Response(503)
    try:
        async with httpx.AsyncClient(
            base_url="https://api.sportsgameodds.com/v2",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = SportsGameOddsClient(
                "test", client=client, owner_limits=limits,
                monthly_entity_reserve=0, request_cooldown_seconds=0,
            )
            with pytest.raises(httpx.HTTPStatusError):
                await provider.get_events(league_ids=["NBA"], limit=1)
            with pytest.raises(QuotaReserveReached, match="Owner-configured"):
                await provider.get_events(league_ids=["NBA"], limit=1)
        assert events == ["/v2/events"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_live_owner_limit_change_takes_effect_without_restart(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "quota.sqlite"))
    limits = OwnerLimitStore(store)
    limits.save(OwnerLimit("sportsgameodds", "free", 101, 0))
    calls = []
    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/usage"):
            return httpx.Response(200, json=usage(current=100))
        return httpx.Response(200, json={"success": True, "data": []})
    try:
        async with httpx.AsyncClient(
            base_url="https://api.sportsgameodds.com/v2",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = SportsGameOddsClient(
                "test", client=client, owner_limits=limits,
                monthly_entity_reserve=0, request_cooldown_seconds=0,
            )
            await provider.get_events(league_ids=["NBA"], limit=1)
            with pytest.raises(QuotaReserveReached):
                await provider.get_events(league_ids=["NBA"], limit=1)
            limits.save(OwnerLimit("sportsgameodds", "paid", 105, 0))
            await provider.get_events(league_ids=["NBA"], limit=2)
        assert calls.count("/v2/events") == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_verified_unlimited_paid_entities_allow_market_reads_without_local_owner_cap():
    calls = []
    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/usage"):
            return httpx.Response(200, json=usage(
                current="n/a", maximum="unlimited",
            ))
        return httpx.Response(200, json={"success": True, "data": []})
    async with httpx.AsyncClient(
        base_url="https://api.sportsgameodds.com/v2",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = SportsGameOddsClient(
            "test", client=client, monthly_entity_reserve=250,
            request_cooldown_seconds=0,
        )
        await provider.get_events(league_ids=["NBA"], limit=8)
    assert calls == ["/v2/account/usage", "/v2/events"]


@pytest.mark.asyncio
async def test_unlimited_provider_with_owner_cap_still_requires_observable_usage(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "paid.sqlite"))
    limits = OwnerLimitStore(store)
    limits.save(OwnerLimit("sportsgameodds", "paid", 110, 0))
    seen = []
    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=usage(current="n/a", maximum="unlimited"))
    try:
        async with httpx.AsyncClient(
            base_url="https://api.sportsgameodds.com/v2",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = SportsGameOddsClient(
                "test", client=client, owner_limits=limits,
                monthly_entity_reserve=0, request_cooldown_seconds=0,
            )
            with pytest.raises(QuotaReserveReached, match="cannot enforce owner budget"):
                await provider.get_events(league_ids=["NBA"], limit=2)
        assert seen == ["/v2/account/usage"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_verified_rolling_month_end_resets_only_when_provider_reports_next_period():
    from datetime import UTC, datetime, timedelta

    first = datetime(2026, 11, 12, tzinfo=UTC)
    second = first + timedelta(days=30)
    period = [first.isoformat()]
    calls = []
    def handler(req):
        if req.url.path.endswith("/usage"):
            report = usage(current=2497, maximum=2500)
            report["data"]["rateLimits"]["per-month"]["currentIntervalEndTime"] = period[0]
            return httpx.Response(200, json=report)
        calls.append(req.url.params["limit"])
        return httpx.Response(200, json={"success": True, "data": []})
    async with httpx.AsyncClient(
        base_url="https://api.sportsgameodds.com/v2",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = SportsGameOddsClient(
            "test", client=client, monthly_entity_reserve=0,
            request_cooldown_seconds=0,
        )
        await provider.get_events(league_ids=["NBA"], limit=3)
        with pytest.raises(QuotaReserveReached):
            await provider.get_events(league_ids=["NBA"], limit=1)
        period[0] = second.isoformat()
        # New provider window should report a fresh count, not last period's.
        # This fixture does not lower current to make the test meaningful.
        await provider.get_events(league_ids=["NBA"], limit=3)
    assert calls == ["3", "3"]
