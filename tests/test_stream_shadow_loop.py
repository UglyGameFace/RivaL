"""The second paired-quote ledger gets a fresh book, and a stale one is a skip."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals.live_quotes import QuoteUnavailable


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
        assets=("ETH",), snapshot=lambda venue, asset: next(snapshots[venue]))
    return client


def test_second_ledger_reads_a_fresh_book():
    seen = []

    def multi_observe(book):
        seen.append(book)
        return {"opened": 0, "closed": 0}

    snapshots = {"hyperliquid": iter(["old-hl", "fresh-hl"]),
                 "binance": iter(["old-bn", "fresh-bn"])}
    asyncio.run(_client(snapshots, multi_observe)._stream_shadow_loop())
    assert seen == ["fresh-hl", "fresh-bn"]


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
