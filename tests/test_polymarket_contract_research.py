"""A Polymarket choice must produce only exact-contract research and receipts."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.discord_app import polymarket_watch as poly
from parlay_bot.discord_app.polymarket_live_board import live_polymarket_embed
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


@pytest.mark.asyncio
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
    assert "Test call DOWN" in correct.title and "not proven" in correct.title
    assert any("DOWN" in f.value for f in correct.fields if "not proven" in f.name)
    assert "Not proven to make money" in correct.description
    assert "calibrated" not in correct.description
    for foreign in (replace(call, slug="other-market"),
                    replace(call, horizon=15), replace(call, expires=datetime.now(UTC)-timedelta(seconds=1))):
        rejected = live_polymarket_embed(board, foreign, asset="BTC", horizon=5)
        assert "No call yet" in rejected.title
        assert not any("model call" in f.name for f in rejected.fields)


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
    # The slug is bound to the exact hour: its suffix is the hour's epoch
    # bucket (the retired dated name carried the year instead).
    assert slug == f"btc-updown-1h-{int(start.timestamp())}"
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
    # The window deliberately belongs to a past year, but book receipt time
    # must be the same simulated clock, not the CI runner's current date.
    monkeypatch.setattr(reader, "_receipt_clock", lambda: now)
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
    service = PolymarketResearch(hot)
    service.initialize()
    current = notice()
    row = {"condition_id": CONDITION, "status": "resolved", "extended_review": False,
           "transaction_hash": "0x"+"b"*64, "resolved_block": 999,
           "resolved_at": (current.expires+timedelta(seconds=10)).isoformat(),
           "payouts": [0, 1000000]}
    source_call = {"slug": current.slug, "condition_id": CONDITION,
                   "expires_at": current.expires.isoformat()}
    now = current.expires+timedelta(seconds=20)
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
async def test_study_wait_timeout_does_not_release_a_running_worker(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "study-timeout.sqlite")
    service = PolymarketResearch(store)
    loop = asyncio.get_running_loop()
    both_running = asyncio.Event()
    release = threading.Event()
    calls = []

    def study(current):
        calls.append(current.slug)
        if len(calls) == 2:
            loop.call_soon_threadsafe(both_running.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr(service, "study", study)
    current = notice()
    second = replace(current, slug="eth-test-contract", asset="ETH")
    third = replace(current, slug="sol-test-contract", asset="SOL")
    try:
        first_task = service.schedule_study(current, limit=2, cache_call=False)
        second_task = service.schedule_study(second, limit=2, cache_call=False)
        await asyncio.wait_for(both_running.wait(), timeout=2)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(first_task), timeout=0)

        assert not first_task.done()
        assert service.schedule_study(current) is first_task
        assert service.schedule_study(third, limit=2, cache_call=False) is None
        assert sorted(calls) == sorted([current.slug, second.slug])

        release.set()
        assert await asyncio.gather(first_task, second_task) == [True, True]
        assert await service.schedule_study(third, limit=2, cache_call=False) is True
        assert calls.count(third.slug) == 1
    finally:
        release.set()
        await service.aclose()
        store.close()


@pytest.mark.asyncio
async def test_hourly_uma_resolution_is_read_from_settled_price(hot):
    """Hourly Up/Down markets report a UMA price, not a payout vector."""
    service = PolymarketResearch(hot)
    service.initialize()
    current = notice()
    source_call = {"slug": current.slug, "condition_id": CONDITION,
                   "expires_at": current.expires.isoformat()}
    now = current.expires + timedelta(minutes=5)
    row = {"condition_id": CONDITION, "status": "resolved", "extended_review": False,
           "was_disputed": False, "transaction_hash": "0x" + "c" * 64,
           "price": "1000000000000000000", "proposed_price": "1000000000000000000",
           "last_update_timestamp": str(int((current.expires + timedelta(minutes=1)).timestamp()))}
    assert service._record_resolution(source_call, {**row, "price": "500000000000000000"},
                                      now=now) == 0
    assert service._record_resolution(source_call, {**row, "was_disputed": True}, now=now) == 0
    early = str(int((current.expires - timedelta(minutes=1)).timestamp()))
    assert service._record_resolution(source_call, {**row, "last_update_timestamp": early},
                                      now=now) == 0
    assert service._record_resolution(source_call, row, now=now) == 1
    with hot.connect() as conn:
        saved = conn.execute("SELECT * FROM rival_polymarket_ai_outcomes").fetchone()
    assert saved["direction"] == "UP"
    await service.aclose()


def test_unsettled_dedupes_across_sources_and_keeps_rotation(hot):
    """_unsettled returns each distinct slug once, least-recently-checked first.

    The same contract sits in ai_calls (twice: an original and a
    revision), in observations and in contest rounds; the UNION ALL +
    Python dedupe must return it once, exclude resolved slugs, keep
    the 20 cap and the checked_at rotation, and stamp resolution
    attempts for exactly the returned slugs.
    """
    import uuid

    prefix = "tu" + uuid.uuid4().hex[:8]
    dup, done = f"{prefix}-dup", f"{prefix}-done"
    plain = [f"{prefix}-s{i:02d}" for i in range(25)]
    with hot.connect() as conn:
        for table in ("rival_polymarket_ai_calls", "rival_polymarket_ai_outcomes",
                      "rival_polymarket_observations", "rival_poly_contest_rounds",
                      "rival_polymarket_resolution_attempts"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    service = PolymarketResearch(hot)
    service.initialize()
    now = datetime(2026, 11, 18, 5, 0, tzinfo=UTC)
    base = now - timedelta(hours=2)

    def call_row(slug, expires, sha):
        return (slug, "BTC", 15, f"cond-{slug}",
                (expires - timedelta(minutes=15)).isoformat(), expires.isoformat(),
                (expires - timedelta(minutes=14)).isoformat(),
                "m", "UP", "t", "r", sha, "{}")

    def obs_row(slug, expires):
        return (slug, f"cond-{slug}", "BTC", 15, "src", expires.isoformat(),
                now.isoformat(), "{}", f"sha-{slug}", "{}", None)

    def round_row(slug, expires):
        return (slug, f"cond-{slug}", "BTC", 15, "k",
                (expires - timedelta(minutes=15)).isoformat(), expires.isoformat(),
                (expires - timedelta(minutes=14)).isoformat(), now.isoformat(),
                0.5, None, None, f"sha-{slug}", "{}")

    with hot.connect() as conn:
        conn.execute("INSERT INTO rival_polymarket_ai_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     call_row(dup, base, "sha-a"))
        conn.execute("INSERT INTO rival_polymarket_ai_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     call_row(dup, base, "sha-b"))
        conn.execute("INSERT INTO rival_polymarket_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     obs_row(dup, base))
        conn.execute("INSERT INTO rival_poly_contest_rounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     round_row(dup, base))
        conn.execute("INSERT INTO rival_polymarket_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     obs_row(done, base + timedelta(minutes=5)))
        conn.execute("INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
                     (done, f"cond-{done}", "UP", now.isoformat(), now.isoformat(), "sha", "{}"))
        for i, slug in enumerate(plain):
            conn.execute("INSERT INTO rival_polymarket_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         obs_row(slug, base + timedelta(minutes=10 + i)))
        conn.execute("INSERT INTO rival_polymarket_resolution_attempts VALUES (?,?)",
                     (plain[3], (now - timedelta(hours=1)).isoformat()))

    result = service._unsettled(now)
    slugs = [row["slug"] for row in result]
    assert slugs[0] == dup
    assert slugs.count(dup) == 1
    assert done not in slugs
    assert len(slugs) == 20
    assert slugs[1:4] == plain[0:3]
    assert slugs[-1] == plain[19]
    assert plain[3] not in slugs  # checked an hour ago: rotated behind unchecked
    with hot.connect() as conn:
        attempts = {row["slug"]: row["checked_at"] for row in conn.execute(
            "SELECT slug,checked_at FROM rival_polymarket_resolution_attempts").fetchall()}
    assert attempts[dup] == now.isoformat()
    assert attempts[plain[0]] == now.isoformat()
    assert attempts[plain[3]] == (now - timedelta(hours=1)).isoformat()
    assert done not in attempts


def test_unsettled_excludes_resolved_slugs_from_every_source(hot):
    """A slug with a recorded outcome is never returned, whichever source
    table still carries it: the outcome anti-join applies inside each
    UNION branch, not just to the observations branch. An unresolved
    contest-round-only slug is still returned."""
    import uuid

    prefix = "tu" + uuid.uuid4().hex[:8]
    done, open_slug = f"{prefix}-done", f"{prefix}-open"
    with hot.connect() as conn:
        for table in ("rival_polymarket_ai_calls", "rival_polymarket_ai_outcomes",
                      "rival_polymarket_observations", "rival_poly_contest_rounds",
                      "rival_polymarket_resolution_attempts"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    service = PolymarketResearch(hot)
    service.initialize()
    now = datetime(2026, 11, 18, 5, 0, tzinfo=UTC)
    base = now - timedelta(hours=2)
    with hot.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_ai_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (done, "BTC", 15, f"cond-{done}",
             (base - timedelta(minutes=15)).isoformat(), base.isoformat(),
             (base - timedelta(minutes=14)).isoformat(),
             "m", "UP", "t", "r", "sha", "{}"))
        for slug, expires in ((done, base), (open_slug, base + timedelta(minutes=5))):
            conn.execute(
                "INSERT INTO rival_poly_contest_rounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, f"cond-{slug}", "BTC", 15, "k",
                 (expires - timedelta(minutes=15)).isoformat(), expires.isoformat(),
                 (expires - timedelta(minutes=14)).isoformat(), now.isoformat(),
                 0.5, None, None, "sha", "{}"))
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
            (done, f"cond-{done}", "UP", now.isoformat(), now.isoformat(), "sha", "{}"))
    result = service._unsettled(now)
    assert [row["slug"] for row in result] == [open_slug]
