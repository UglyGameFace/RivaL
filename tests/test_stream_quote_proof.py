"""Integration contracts: independent live books and strictly paired forward P&L."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import BookQuote, PerpReferenceBook, PublicBestQuoteClient
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.stream_quotes import StreamingBookHub, parse_stream_message
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def hyper(at, *, bid=100, ask=100.1, asset="BTC"):
    return PerpReferenceBook(
        asset, "hyperliquid", bid, ask, 100.0, 100.0, at, at,
    )


def binance(at, *, bid=100, ask=100.1, asset="BTC"):
    return BookQuote(
        asset, "binance_usdm", asset + "USDT", bid, 100, ask, 100, at, at,
    )


def wire_hyper(at, *, coin="BTC", bid="100", ask="100.1"):
    return json.dumps({
        "channel": "l2Book",
        "data": {
            "coin": coin, "time": int(at.timestamp() * 1000),
            "levels": [
                [{"px": bid, "sz": "100", "n": 1}],
                [{"px": ask, "sz": "100", "n": 1}],
            ],
        },
    })


def wire_binance(at, *, symbol="BTCUSDT", bid="100", ask="100.1", st=1):
    return json.dumps({
        "stream": f"{symbol.lower()}@bookTicker",
        "data": {
            "e": "bookTicker", "E": int(at.timestamp() * 1000),
            "s": symbol, "st": st,
            "b": bid, "B": "100", "a": ask, "A": "100",
        },
    })


def forecast(store, at, *, venue="hyperliquid", horizon=5, probability=.7):
    identity = {
        "hyperliquid": ("BTC", "USDC"),
        "binance": ("BTCUSDT", "USDT"),
    }
    contract, quote = identity[venue]
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,
                target_opened_at,probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, contract, quote, "BTC", horizon, MODEL,
                (at - timedelta(minutes=5)).isoformat(), 100.0, at.isoformat(),
                (at + timedelta(minutes=horizon - 5)).isoformat(),
                probability, 0.5, 90,
            ),
        )


def test_order_book_wire_frames_validate_source_clock_and_market_identity():
    now = datetime.now(UTC).replace(microsecond=0)
    assert parse_stream_message(
        "hyperliquid", wire_hyper(now), received_at=now,
    ).venue == "hyperliquid"
    assert parse_stream_message(
        "binance", wire_binance(now), received_at=now,
    ).venue == "binance_usdm"
    assert parse_stream_message(
        "binance", wire_binance(now, st=2), received_at=now,
    ) is None
    assert parse_stream_message(
        "hyperliquid",
        json.dumps({"channel": "subscriptionResponse", "data": {}}),
        received_at=now,
    ) is None
    assert parse_stream_message(
        "binance", wire_binance(now, symbol="DOGEUSDT"), received_at=now,
    ) is None
    from parlay_bot.perpetuals.live_quotes import QuoteUnavailable

    with pytest.raises(QuoteUnavailable):
        parse_stream_message(
            "binance", wire_binance(now - timedelta(minutes=2)), received_at=now,
        )
    with pytest.raises(QuoteUnavailable):
        parse_stream_message(
            "hyperliquid", wire_hyper(now, bid="120", ask="100"),
            received_at=now,
        )
    with pytest.raises(QuoteUnavailable):
        parse_stream_message("hyperliquid", "not json", received_at=now)
    with pytest.raises(QuoteUnavailable):
        parse_stream_message("binance", b"binary", received_at=now)


def test_ram_cache_does_not_resurrect_old_tick_or_replayed_book():
    now = datetime.now(UTC).replace(microsecond=0)
    hub = StreamingBookHub(assets=("BTC",))
    assert hub.accept("hyperliquid", wire_hyper(now), received_at=now)
    assert not hub.accept("hyperliquid", wire_hyper(now), received_at=now)
    assert hub.snapshot("hyperliquid", "BTC", now=now) is not None
    assert hub.snapshot(
        "hyperliquid", "BTC", now=now + timedelta(seconds=11),
    ) is None
    assert not hub.accept(
        "binance", wire_binance(now + timedelta(minutes=1)), received_at=now,
    )
    assert hub.health(now=now)["hyperliquid"]["fresh_assets"] == ("BTC",)
    assert hub.health(now=now + timedelta(seconds=11))["hyperliquid"]["fresh_assets"] == ()


@pytest.mark.asyncio
async def test_existing_live_quote_client_prefers_stream_without_rest_requests():
    now = datetime.now(UTC).replace(microsecond=0)
    hub = StreamingBookHub(assets=("BTC",))
    assert hub.accept("binance", wire_binance(now), received_at=now)
    assert hub.accept("hyperliquid", wire_hyper(now), received_at=now)
    import httpx

    def forbidden(_request):
        raise AssertionError("fresh streams must not trigger REST calls")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(forbidden), base_url="https://fapi.binance.com",
    )
    reference = httpx.AsyncClient(
        transport=httpx.MockTransport(forbidden),
        base_url="https://api.hyperliquid.xyz",
    )
    quotes = PublicBestQuoteClient(client=client, reference_client=reference)
    quotes.stream_hub = hub
    try:
        assert (await quotes.get("BTC")).venue == "binance_usdm"
        assert (await quotes.get_reference("BTC")).venue == "hyperliquid"
    finally:
        await quotes.aclose()
        await client.aclose()
        await reference.aclose()


def test_strict_prospective_venue_matching_and_realized_after_cost_losses(tmp_path):
    store = SQLiteHotStore(tmp_path / "shadow.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    proof = StreamShadowLedger(store)
    issued = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=6)
    entry = issued + timedelta(seconds=20)
    exit_at = issued + timedelta(minutes=5, seconds=20)
    try:
        forecast(store, issued, venue="hyperliquid", probability=.75)
        assert proof.observe(binance(entry), now=entry)["opened"] == 0
        assert proof.observe(hyper(entry), now=entry)["opened"] == 1
        assert proof.observe(hyper(entry), now=entry)["opened"] == 0
        assert proof.observe(binance(exit_at), now=exit_at)["closed"] == 0
        observed = proof.observe(
            hyper(exit_at, bid=99.0, ask=99.1), now=exit_at,
        )
        assert observed["closed"] == 1
        report = proof.report(
            asset="BTC", horizon_minutes=5, now=issued + timedelta(minutes=9),
        )
        assert report["eligible_windows"] == 1
        assert report["entries"] == 1 and report["closed"] == 1
        assert report["coverage"] == 1
        assert report["costs"]["conservative"]["mean_net_bps"] < 0
        assert report["status"] == "NOT_PROVEN"
        assert not report["real_fills_verified"]
        restarted = StreamShadowLedger(SQLiteHotStore(tmp_path / "shadow.sqlite"))
        assert restarted.report(
            asset="BTC", horizon_minutes=5, now=issued + timedelta(minutes=9),
        )["closed"] == 1
    finally:
        store.close()


def test_short_costs_and_missed_exits_are_not_backfilled(tmp_path):
    store = SQLiteHotStore(tmp_path / "short.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    proof = StreamShadowLedger(store)
    issued = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    entry = issued + timedelta(seconds=15)
    try:
        forecast(store, issued, venue="binance", probability=.2)
        assert proof.observe(binance(entry), now=entry)["opened"] == 1
        assert proof.observe(
            hyper(issued + timedelta(minutes=5, seconds=10)),
            now=issued + timedelta(minutes=5, seconds=10),
        )["closed"] == 0
        assert proof.expire(now=issued + timedelta(minutes=7)) == 1
        assert proof.observe(
            binance(issued + timedelta(minutes=8), bid=50, ask=50.1),
            now=issued + timedelta(minutes=8),
        )["closed"] == 0
        report = proof.report(
            asset="BTC", horizon_minutes=5, now=issued + timedelta(minutes=9),
        )
        assert report["closed"] == 0
        assert report["missed_exits"] == 1
        assert report["coverage"] == 0
    finally:
        store.close()


def test_paired_short_has_correct_entry_proceeds_denominator(tmp_path):
    store = SQLiteHotStore(tmp_path / "shortpaired.sqlite")
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    proof = StreamShadowLedger(store)
    issued = datetime.now(UTC) - timedelta(minutes=6)
    try:
        forecast(store, issued, venue="binance", probability=.2)
        entry = issued + timedelta(seconds=12)
        exit_at = issued + timedelta(minutes=5, seconds=12)
        proof.observe(binance(entry, bid=100, ask=100.2), now=entry)
        assert proof.observe(
            binance(exit_at, bid=99, ask=99.1), now=exit_at,
        )["closed"] == 1
        out = proof.report(
            asset="BTC", horizon_minutes=5, now=issued + timedelta(minutes=9),
        )
        assert out["costs"]["conservative"]["mean_net_bps"] == pytest.approx(
            90 - 14,
        )
    finally:
        store.close()


def test_postgres_restart_and_idempotent_same_venue_forward_proof():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL 16 integration service")
    store = PostgresHotStore(dsn)
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    proof = StreamShadowLedger(store)
    issued = datetime.now(UTC) - timedelta(minutes=6)
    try:
        forecast(store, issued, venue="hyperliquid", probability=.74)
        entry = issued + timedelta(seconds=10)
        closed_at = issued + timedelta(minutes=5, seconds=10)
        assert proof.observe(hyper(entry), now=entry)["opened"] == 1
        other = StreamShadowLedger(PostgresHotStore(dsn))
        assert other.observe(hyper(entry), now=entry)["opened"] == 0
        assert other.observe(
            hyper(closed_at, bid=101, ask=101.1), now=closed_at,
        )["closed"] == 1
        assert other.report(
            asset="BTC", horizon_minutes=5, now=issued + timedelta(minutes=9),
        )["closed"] >= 1
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM rival_stream_shadow WHERE asset='BTC' "
                "AND venue='hyperliquid' AND target_close_at=?",
                ((issued + timedelta(minutes=5)).isoformat(),),
            )
            conn.execute(
                "DELETE FROM perp_forward_audit WHERE venue='hyperliquid' "
                "AND contract='BTC' AND reference_opened_at=?",
                ((issued - timedelta(minutes=5)).isoformat(),),
            )
        store.close()


@pytest.mark.asyncio
async def test_hub_shutdown_is_cancellation_safe(monkeypatch):
    started = asyncio.Event()

    async def stalled(_venue):
        started.set()
        await asyncio.Future()

    hub = StreamingBookHub(assets=("BTC",))
    monkeypatch.setattr(hub, "_run", stalled)
    hub.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(hub.aclose(), timeout=1)
    assert hub._tasks == []


@pytest.mark.asyncio
async def test_websocket_subscription_reconnect_and_stop_without_network(monkeypatch):
    from parlay_bot.perpetuals import stream_quotes

    sessions = []

    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.number = len(sessions) + 1
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        def __aiter__(self):
            async def events():
                yield wire_hyper(datetime.now(UTC))
                if self.number > 1:
                    await asyncio.Future()  # hold the restored WS until shutdown
            return events()

    def fake_connect(url, **kwargs):
        assert url == stream_quotes.HYPERLIQUID_WS
        assert kwargs["max_queue"] <= 32
        assert kwargs["open_timeout"] <= 10
        return FakeSocket()

    monkeypatch.setattr(stream_quotes.websockets, "connect", fake_connect)
    monkeypatch.setattr(stream_quotes.random, "uniform", lambda _a, _b: 0)
    hub = StreamingBookHub(assets=("BTC", "ETH"))
    task = asyncio.create_task(hub._run("hyperliquid"))
    try:
        async def until_reconnected():
            while len(sessions) < 2:
                await asyncio.sleep(.02)
        await asyncio.wait_for(until_reconnected(), timeout=4)
        assert hub.health()["hyperliquid"]["reconnects"] >= 1
        assert {item["subscription"]["coin"] for item in sessions[0].sent} == {
            "BTC", "ETH",
        }
        assert hub.snapshot("hyperliquid", "BTC") is not None
    finally:
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not hub.health()["hyperliquid"]["connected"]
