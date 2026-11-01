"""A Polymarket choice must produce only exact-contract research and receipts."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.discord_app import polymarket_watch as poly
from parlay_bot.discord_app.polymarket_live_board import live_polymarket_embed
from parlay_bot.perpetuals import polymarket_research as research_module
from parlay_bot.perpetuals.polymarket_research import ContractCall, PolymarketResearch
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

CONDITION = "0x" + "a" * 64


def notice(now=None):
    now = now or datetime.now(UTC)
    start = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    # A source test fixture with ample AI execution time, independent of boundary.
    return poly.PolymarketNotice(
        asset="BTC", horizon=5, slug=poly.market_slug("BTC", 5, start),
        issued=start, expires=now + timedelta(minutes=4),
        quote_at=now, received_at=now, up_ask=.60, down_ask=.42,
        up_average_ask=.61, down_average_ask=.43,
        condition_id=CONDITION, up_token_id="1" * 60, down_token_id="2" * 60,
        resolution_source="https://data.chain.link/streams/btc-usd-twap-60s-streams",
    )


@pytest.fixture(params=["sqlite", "postgres"])
def hot(request, tmp_path):
    if request.param == "postgres":
        dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("PostgreSQL integration database unavailable")
        store = PostgresHotStore(dsn)
        with store.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS rival_polymarket_ai_calls")
            conn.execute("DROP TABLE IF EXISTS rival_polymarket_ai_outcomes")
    else:
        store = SQLiteHotStore(tmp_path / "contract-ai.sqlite")
    store.initialize()
    yield store
    store.close()


def ai():
    async def review(evidence):
        assert evidence["target"] == "polymarket"
        assert evidence["resolution_source"].startswith("https://data.chain.link/")
        assert "bitget" not in json.dumps(evidence)
        assert "sources" not in evidence  # no independent perp candle summaries
        return {"slug": evidence["slug"], "direction": "up", "model": "verified:free",
                "thesis": "Experimental contract assessment", "risk": "Unvalidated"}
    return SimpleNamespace(available=True, review_contract=AsyncMock(side_effect=review))


@pytest.mark.asyncio
async def test_contract_call_is_saved_once_and_survives_restart(hot):
    reviewer = ai()
    service = PolymarketResearch(hot, ai=reviewer)
    current = notice()
    assert service.observe(current, request_ai=True) is None
    assert service.observe(current, request_ai=True) is None
    call = await service.wait(current.slug)
    assert call.direction == "UP" and call.slug == current.slug
    assert call.expires == current.expires
    reviewer.review_contract.assert_awaited_once()
    restarted = PolymarketResearch(hot, ai=ai())
    restarted.observe(current, request_ai=True)
    assert await restarted.wait(current.slug) == call
    restarted.ai.review_contract.assert_not_awaited()
    with hot.connect() as conn:
        row = conn.execute("SELECT * FROM rival_polymarket_ai_calls").fetchone()
        evidence = json.loads(row["evidence_json"])
    assert evidence["slug"] == call.slug
    assert evidence["condition_id"] == CONDITION
    assert "contract_outcome_training_verified" in evidence
    await service.aclose()
    await restarted.aclose()


@pytest.mark.asyncio
async def test_foreign_or_expired_ai_reply_cannot_become_a_contract_call(hot):
    reviewer = ai()
    reviewer.review_contract.side_effect = None
    reviewer.review_contract.return_value = {
        "slug": "another-window", "direction": "up", "model": "test:free",
        "thesis": "Wrong target", "risk": "No match",
    }
    service = PolymarketResearch(hot, ai=reviewer)
    current = notice()
    service.observe(current, request_ai=True)
    assert await service.wait(current.slug) is None
    assert "did not match" in service.status(current.slug)
    assert service._saved(current.slug) is None
    await service.aclose()


@pytest.mark.asyncio
async def test_stale_or_incomplete_inputs_never_consume_ai_quota(hot):
    service = PolymarketResearch(hot, ai=ai())
    current = notice()
    for bad in (replace(current, quote_at=current.quote_at-timedelta(seconds=26)),
                replace(current, condition_id=None),
                replace(current, expires=current.quote_at-timedelta(seconds=1))):
        assert service.observe(bad, request_ai=True) is None
    service.ai.review_contract.assert_not_awaited()
    assert not service._pending
    await service.aclose()


def test_issued_call_only_matches_its_exact_source_window():
    current = notice()
    listing = poly.PolymarketListedWindow(
        current.asset, current.horizon, current.slug, current.issued,
        current.expires, True, None,
    )
    board = poly.PolymarketWindowBoard(datetime.now(UTC), listing, None, current)
    call = ContractCall(current.slug, current.asset, current.horizon,
                        datetime.now(UTC), current.expires, "DOWN", "test:free",
                        "Exact-contract assessment", "Unvalidated", "a" * 64)
    correct = live_polymarket_embed(board, call, asset="BTC", horizon=5)
    assert "AI DOWN" in correct.title
    assert "Experimental" in correct.description
    assert "calibrated" not in correct.description
    for foreign in (replace(call, slug="other-market"),
                    replace(call, horizon=15), replace(call, expires=datetime.now(UTC)-timedelta(seconds=1))):
        rejected = live_polymarket_embed(board, foreign, asset="BTC", horizon=5)
        assert "AI WAIT" in rejected.title


@pytest.mark.asyncio
async def test_one_cancelled_menu_does_not_cancel_shared_quote(monkeypatch):
    reader = poly.PolymarketReader()
    current = notice()
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(**kwargs):
        started.set()
        await release.wait()
        return current

    lookup_mock = AsyncMock(side_effect=lookup)
    monkeypatch.setattr(reader, "_lookup", lookup_mock)
    first = asyncio.create_task(reader.market("BTC", 5))
    await started.wait()
    second = asyncio.create_task(reader.market("BTC", 5))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second is current
    lookup_mock.assert_awaited_once()
    await reader.aclose()


@pytest.mark.asyncio
async def test_hourly_market_requires_current_year_time_and_real_binance_rule(monkeypatch):
    # Pin the market window so CI cannot cross an hour while this test runs.
    start = datetime(2026, 10, 30, 12, tzinfo=UTC)
    now = start + timedelta(minutes=10)
    slug = poly.market_slug("BTC", 60, start)
    assert f"-{start.year}-" in slug
    controls = {"year": 0, "source": "https://www.binance.com/en/trade/BTC_USDT"}

    def handle(req):
        if req.url.host == "gamma-api.polymarket.com":
            opened = start.replace(year=start.year + controls["year"])
            return httpx.Response(200, json={
                "slug": slug, "active": True, "closed": False, "archived": False,
                "endDate": (opened+timedelta(hours=1)).isoformat(),
                "markets": [{"active": True, "closed": False, "acceptingOrders": True,
                             "enableOrderBook": True, "eventStartTime": opened.isoformat(),
                             "outcomes": '["Up","Down"]', "conditionId": CONDITION,
                             "clobTokenIds": json.dumps(["1"*60, "2"*60]),
                             "resolutionSource": controls["source"]}],
            })
        return httpx.Response(200, json={
            "asset_id": req.url.params["token_id"], "market": CONDITION,
            "timestamp": str(int(now.timestamp()*1000)),
            "asks": [{"price": ".55", "size": "20"}],
        })

    actual = httpx.AsyncClient
    monkeypatch.setattr(poly.httpx, "AsyncClient", lambda *a, **kw: actual(
        transport=httpx.MockTransport(handle), timeout=3.5,
    ))
    reader = poly.PolymarketReader()
    try:
        assert await reader.market("BTC", 60, now=now) is not None
        controls["year"] = -1
        assert await reader.market("BTC", 60, now=now, refresh=True) is None
        controls.update(year=0, source="https://bitget.example/candle")
        assert await reader.market("BTC", 60, refresh=True) is None
    finally:
        await reader.aclose()


@pytest.mark.asyncio
async def test_terminal_resolution_receipt_matches_forecast_and_is_immutable(hot):
    service = PolymarketResearch(hot, ai=ai())
    current = notice()
    service.observe(current, request_ai=True)
    call = await service.wait(current.slug)
    row = {"condition_id": CONDITION, "status": "resolved", "extended_review": False,
           "transaction_hash": "0x"+"b"*64, "resolved_block": 999,
           "resolved_at": (call.expires+timedelta(seconds=10)).isoformat(),
           "payouts": [0, 1000000]}
    source_call = {"slug": call.slug, "condition_id": CONDITION,
                   "expires_at": call.expires.isoformat()}
    now = call.expires+timedelta(seconds=20)
    assert service._record_resolution(source_call, {**row, "status": "proposed"}, now=now) == 0
    assert service._record_resolution(source_call, {**row, "condition_id": "wrong"}, now=now) == 0
    assert service._record_resolution(source_call, {**row, "payouts": [500000, 500000]}, now=now) == 0
    assert service._record_resolution(source_call, row, now=now) == 1
    assert service._record_resolution(source_call, {**row, "payouts": [1, 0]}, now=now) == 0
    with hot.connect() as conn:
        saved = conn.execute("SELECT * FROM rival_polymarket_ai_outcomes").fetchone()
    assert saved["direction"] == "DOWN"
    assert json.loads(saved["receipt_json"])["payouts"] == [0, 1000000]
    await service.aclose()


@pytest.mark.asyncio
async def test_meaningful_live_change_can_append_an_immutable_revision(hot, monkeypatch):
    clock = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(research_module, "datetime", Clock)
    current = notice(clock[0])
    reviewer = ai()
    service = PolymarketResearch(hot, ai=reviewer)
    service.observe(current, request_ai=True)
    first = await service.wait(current.slug)
    clock[0] += timedelta(seconds=61)
    changed = replace(current, up_ask=.72, down_ask=.30, quote_at=clock[0], received_at=clock[0])
    assert service.observe(changed, request_ai=True) is first
    second = await service.wait(current.slug)
    assert second.issued_at > first.issued_at
    assert second.evidence_sha256 != first.evidence_sha256
    with hot.connect() as conn:
        rows = conn.execute("SELECT * FROM rival_polymarket_ai_calls ORDER BY issued_at").fetchall()
    assert len(rows) == 2
    assert json.loads(rows[0]["evidence_json"])["up_ask"] == .60
    assert json.loads(rows[1]["evidence_json"])["up_ask"] == .72
    assert reviewer.review_contract.await_count == 2
    await service.aclose()


@pytest.mark.asyncio
async def test_background_polymarket_board_never_uses_free_ai_budget(hot):
    reviewer = ai()
    service = PolymarketResearch(hot, ai=reviewer)
    current = notice()
    assert service.observe(current) is None
    assert service.observe(current) is None
    # Learning and training remain independent of optional LLM quota.
    if service._learning_tasks:
        await asyncio.gather(*service._learning_tasks.values())
    reviewer.review_contract.assert_not_awaited()
    assert not service._pending
    assert service._saved(current.slug) is None
    with hot.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_polymarket_observations WHERE slug=?",
            (current.slug,),
        ).fetchone()["n"] == 1
    # One explicit choice can initiate exactly one AI review.
    assert service.observe(current, request_ai=True) is None
    call = await service.wait(current.slug)
    assert call is not None
    reviewer.review_contract.assert_awaited_once()
    await service.aclose()
