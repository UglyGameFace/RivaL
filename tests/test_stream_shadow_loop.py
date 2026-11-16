"""The second paired-quote ledger gets a fresh book, and a stale one is a skip."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals.live_quotes import QuoteUnavailable
from parlay_bot.perpetuals.stream_shadow import SUPPORTED_VENUES


def _client(snapshots, multi_observe):
    client = object.__new__(RivalDiscordClient)
    closed = iter([False, True])
    client.is_closed = lambda: next(closed)

    async def ready():
        return None

    client.wait_until_ready = ready
    client.settings = SimpleNamespace(rival_stream_shadow_seconds=0)
    client.stream_shadow = SimpleNamespace(
        expire=lambda: None,
        observe=lambda book: {"opened": 0, "closed": 0, "expired": 0})
    client.autonomous = SimpleNamespace(multi=SimpleNamespace(
        expire=lambda: None, observe=multi_observe))
    client.live_stream = SimpleNamespace(
        assets=("ETH",), snapshot=lambda venue, asset: next(snapshots[venue])
        if venue in snapshots else None)
    return client


def test_second_ledger_reads_a_fresh_book():
    seen = []

    def multi_observe(book):
        seen.append(book)
        return {"opened": 0, "closed": 0}

    snapshots = {"hyperliquid": iter(["old-hl", "fresh-hl"]),
                 "binance": iter(["old-bn", "fresh-bn"])}
    asyncio.run(_client(snapshots, multi_observe)._stream_shadow_loop())
    assert sorted(seen) == ["fresh-bn", "fresh-hl"]


def test_a_stale_book_is_a_skipped_sample_not_an_error(caplog):
    def multi_observe(book):
        raise QuoteUnavailable("Perp reference is stale.")

    snapshots = {"hyperliquid": iter(["a", "b"]), "binance": iter(["c", "d"])}
    with caplog.at_level(logging.INFO, logger="parlay_bot.discord_app.bot"):
        asyncio.run(_client(snapshots, multi_observe)._stream_shadow_loop())
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert sum("sample skipped" in r.getMessage() for r in caplog.records) == 2


def test_a_book_gone_by_the_second_read_is_simply_not_sampled():
    seen = []
    snapshots = {"hyperliquid": iter(["a", None]), "binance": iter(["c", "d"])}

    def multi_observe(book):
        seen.append(book)
        return {"opened": 0, "closed": 0}

    asyncio.run(_client(snapshots, multi_observe)._stream_shadow_loop())
    assert seen == ["d"]


def test_expire_prepares_family_schema_before_concurrent_quote_sampling(learning_store):
    """The new worker cap must not race PostgreSQL/SQLite schema creation."""
    from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger

    ledger = StreamShadowLedger(learning_store)
    assert not ledger._family_quote_shadow._initialized
    ledger.expire(now=datetime.now(UTC))
    assert ledger._initialized
    assert ledger._family_quote_shadow._initialized


def test_tail_native_book_is_seen_even_while_first_exchange_is_slow():
    """No source waits behind 28 serial PostgreSQL operations."""
    release = threading.Event()
    observed_before_release = []

    def observe(book):
        if book == "hyperliquid":
            observed_before_release.append(release.wait(timeout=1))
        if book == "gate":
            release.set()
        return {"opened": 0, "closed": 0, "expired": 0}

    snapshots = {v: iter((v, v)) for v in SUPPORTED_VENUES}
    client = _client(snapshots, lambda _: {"opened": 0, "closed": 0})
    client.stream_shadow.observe = observe
    asyncio.run(client._stream_shadow_loop())
    assert observed_before_release == [True]
    assert release.is_set()


def test_quote_observer_is_bounded_by_three_concurrent_database_writers():
    """Keep spare pool capacity for the production learner and Discord."""
    running = 0
    peak = 0
    seen = set()
    lock = threading.Lock()

    def observe(book):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.025)
        with lock:
            seen.add(book)
            running -= 1
        return {"opened": 0, "closed": 0, "expired": 0}

    client = _client({}, lambda _: {"opened": 0, "closed": 0})
    client.live_stream = SimpleNamespace(
        assets=("BTC", "ETH"),
        snapshot=lambda venue, asset: f"{venue}:{asset}",
    )
    client.stream_shadow.observe = observe
    asyncio.run(client._stream_shadow_loop())
    assert 2 <= peak <= 3
    assert seen == {
        f"{venue}:{asset}" for venue in SUPPORTED_VENUES for asset in ("BTC", "ETH")
    }


def test_each_pass_rotates_source_order_without_scheduling_extra_workers():
    client = _client({}, lambda _: {"opened": 0, "closed": 0})
    states = iter((False, False, True))
    client.is_closed = lambda: next(states)
    seen = []

    async def sample(venue, asset, _permit):
        seen.append((asset, venue))

    client._sample_stream_shadow_quote = sample
    asyncio.run(client._stream_shadow_loop())
    count = len(SUPPORTED_VENUES)
    assert seen[:count] == [("ETH", v) for v in SUPPORTED_VENUES]
    rotated = SUPPORTED_VENUES[1:] + SUPPORTED_VENUES[:1]
    assert seen[count:] == [("ETH", v) for v in rotated]
