"""RIVAL-A: the 5-minute cycle must survive a parked stage instead of
silently missing its heartbeat.

#1: ``_perp_loop`` marks itself background, so every statement on the cycle
path carries a 60 s statement timeout (QueryCanceled) instead of parking
the iteration forever on a Postgres lock.
#2: every stage of ``_perp_loop`` and ``autonomous.cycle()`` carries a
deadline that exceeds the 60 s statement timeout, so a parked statement
aborts first and the deadline only catches non-DB hangs; a timed-out stage
is logged and skipped and the iteration still heartbeats.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from types import SimpleNamespace

import psycopg
import pytest

from parlay_bot.discord_app import bot as bot_module
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals import autonomous as autonomous_module
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.storage import query_scope
from parlay_bot.storage.database import PostgresDatabase

HEARTBEAT_SLUG = "rival-5m-model-cycle"


def test_mark_background_sets_limit_and_rejects_bad_values():
    assert (
        contextvars.Context().run(
            lambda: (
                query_scope.mark_background(),
                query_scope.current_statement_timeout_ms(),
            )[1]
        )
        == query_scope.BACKGROUND_STATEMENT_TIMEOUT_MS
    )
    assert query_scope.BACKGROUND_STATEMENT_TIMEOUT_MS == 60_000
    for bad in (0, 99, 600_001):
        with pytest.raises(ValueError):
            contextvars.Context().run(query_scope.mark_background, bad)


@pytest.mark.asyncio
async def test_mark_background_follows_task_and_threads_but_not_other_tasks():
    async def background():
        query_scope.mark_background()
        return await asyncio.to_thread(query_scope.current_statement_timeout_ms)

    async def worker():
        await asyncio.sleep(0)
        return await asyncio.to_thread(query_scope.current_statement_timeout_ms)

    marked, unmarked = await asyncio.gather(
        asyncio.create_task(background(), context=contextvars.Context()),
        asyncio.create_task(worker(), context=contextvars.Context()),
    )
    assert marked == query_scope.BACKGROUND_STATEMENT_TIMEOUT_MS
    assert unmarked is None


def _pg() -> PostgresDatabase:
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL CI service")
    return PostgresDatabase(dsn, max_size=2)


def test_postgres_background_lock_wait_is_cancelled_after_limit():
    """A statement parked on a lock aborts once the background limit applies.

    The locker holds a session-level advisory lock; the unmarked statement
    parks on it indefinitely (fail-before shape), while the same statement
    under mark_background(200) raises QueryCanceled. Releasing the lock
    lets the parked statement finish, proving the park was the lock.
    """
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No PostgreSQL CI service")
    db = _pg()
    locker = psycopg.connect(dsn, autocommit=True)
    try:
        with locker.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(20261107)")
        parked: dict[str, bool] = {}

        def run_unmarked() -> None:
            with db.connect() as conn:
                conn.execute("SELECT pg_advisory_lock(20261107)")
                conn.execute("SELECT pg_advisory_unlock(20261107)")
            parked["done"] = True

        thread = threading.Thread(
            target=lambda: contextvars.Context().run(run_unmarked))
        thread.start()
        thread.join(timeout=2)
        assert thread.is_alive()

        outcome: dict[str, Exception] = {}

        def run_marked() -> None:
            query_scope.mark_background(200)
            with db.connect() as conn:
                conn.execute("SELECT pg_advisory_lock(20261107)")

        def run_marked_capture() -> None:
            try:
                contextvars.Context().run(run_marked)
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                outcome["error"] = exc

        marked = threading.Thread(target=run_marked_capture)
        marked.start()
        marked.join(timeout=5)
        assert not marked.is_alive()
        assert isinstance(outcome.get("error"), psycopg.errors.QueryCanceled)

        with locker.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(20261107)")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert parked.get("done") is True
        # The connection survives and serves an unlimited worker query.
        with db.connect() as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        locker.close()
        db.close()


def _perp_client(monkeypatch, *, collector=None, autonomous=None):
    """One-iteration _perp_loop client: is_closed flips True after the first pass."""
    client = object.__new__(RivalDiscordClient)
    client.settings = SimpleNamespace(rival_perp_collection_seconds=300)
    client.store = SimpleNamespace()
    client.perp_collector = SimpleNamespace(
        assets=("BTC",), providers=("binance",),
        collect_once=collector,
    )
    client.spike_watch = None
    client.autonomous = autonomous
    client.quote_profit = None
    client.live_stream = None

    async def wait_until_ready() -> None:
        return None

    client.wait_until_ready = wait_until_ready
    closed = iter([False, True])
    client.is_closed = lambda: next(closed)
    monkeypatch.setattr(
        bot_module.host_memory, "snapshot",
        lambda: SimpleNamespace(pressure=False, detail="test"),
    )
    monkeypatch.setattr(
        bot_module, "perpetual_evidence_backlog",
        lambda store: {
            "overdue_forecasts": 0, "recoverable_overdue": 0,
            "closed_quote_windows": 0, "neither_traded_quote_windows": 0,
        },
    )

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    return client


async def _run_loop(monkeypatch, client) -> list[str]:
    beats: list[str] = []
    monkeypatch.setattr(bot_module, "heartbeat", lambda slug: beats.append(slug))
    await asyncio.wait_for(client._perp_loop(), timeout=5)
    return beats


async def test_perp_loop_heartbeats_when_collect_stage_times_out(
    monkeypatch, caplog,
):
    """A blocked collect must not park the iteration: the stage deadline
    skips it and the iteration still reaches the heartbeat."""
    blocked = asyncio.Event()

    async def collect_once() -> None:
        await blocked.wait()

    client = _perp_client(monkeypatch, collector=collect_once)
    monkeypatch.setattr(bot_module, "PERP_COLLECT_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.WARNING, logger="parlay_bot.discord_app.bot"):
        beats = await _run_loop(monkeypatch, client)
    assert beats == [HEARTBEAT_SLUG]
    assert "collection stage timed out" in caplog.text


async def test_perp_loop_heartbeats_when_forecast_stage_times_out(
    monkeypatch, caplog,
):
    """A blocked autonomous cycle must not park the iteration either."""
    blocked = asyncio.Event()

    async def cycle() -> tuple[int, int, int]:
        await blocked.wait()
        return 0, 0, 0

    client = _perp_client(
        monkeypatch, collector=_fast_collect, autonomous=SimpleNamespace(cycle=cycle),
    )
    monkeypatch.setattr(bot_module, "PERP_FORECAST_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.WARNING, logger="parlay_bot.discord_app.bot"):
        beats = await _run_loop(monkeypatch, client)
    assert beats == [HEARTBEAT_SLUG]
    assert "autonomous research stage timed out" in caplog.text


async def _fast_collect() -> None:
    return None


def _research(monkeypatch, *, forward=None, auto_ai=None):
    """AutonomousResearch with stubbed stages; _save_cycle captures its flags."""
    saved: dict[str, object] = {}
    system = object.__new__(AutonomousResearch)
    system.forward = SimpleNamespace(
        run_once=forward if forward is not None else (lambda: (0, 0)),
    )
    system.multi = SimpleNamespace(settle=lambda: 0, issue=lambda: 0)
    system.settle_ai = lambda: None
    system.review_model = lambda: 0
    system.evaluate_promotion = lambda: None

    async def run_auto_ai() -> int:
        if auto_ai is None:
            return 0
        return await auto_ai()

    system.run_auto_ai = run_auto_ai
    system._save_cycle = lambda **kwargs: saved.update(kwargs)
    system.events = SimpleNamespace(refresh=lambda: {
        "sources_checked": 0, "sources_ok": 0,
        "events_seen": 0, "source_failures": [],
    })
    return system, saved


async def test_autonomous_cycle_continues_past_timed_out_forward_stage(
    monkeypatch, caplog,
):
    """A blocked forward stage is skipped (math_ok False) and the cycle
    still saves and returns instead of parking."""
    blocked = threading.Event()

    def run_once():
        # Blocking a to_thread worker the way a parked DB call would: the
        # stub runs on a thread, so it waits on a threading event.
        blocked.wait()
        return 0, 0

    system, saved = _research(monkeypatch, forward=run_once)
    monkeypatch.setattr(
        autonomous_module, "AUTONOMOUS_FORWARD_TIMEOUT_SECONDS", 0.05,
    )
    with caplog.at_level(
        logging.WARNING, logger="parlay_bot.perpetuals.autonomous",
    ):
        result = await asyncio.wait_for(system.cycle(), timeout=5)
    assert result == (0, 0, 0)
    assert saved["math_ok"] is False
    assert "forward research stage timed out" in caplog.text
    # Release the orphaned to_thread worker: wait_for abandons the await,
    # not the thread, so the blocked worker must be let out explicitly.
    blocked.set()
    await asyncio.sleep(0.05)


async def test_autonomous_cycle_continues_past_timed_out_auto_ai_stage(
    monkeypatch, caplog,
):
    """A blocked auto-AI stage is skipped (auto_ai_ok False) and the cycle
    still saves and returns."""
    blocked = asyncio.Event()

    async def auto_ai():
        await blocked.wait()
        return 0

    system, saved = _research(monkeypatch, auto_ai=auto_ai)
    monkeypatch.setattr(
        autonomous_module, "AUTONOMOUS_AUTO_AI_TIMEOUT_SECONDS", 0.05,
    )
    with caplog.at_level(
        logging.WARNING, logger="parlay_bot.perpetuals.autonomous",
    ):
        result = await asyncio.wait_for(system.cycle(), timeout=5)
    assert result == (0, 0, 0)
    assert saved["auto_ai_ok"] is False
    assert "automatic AI stage timed out" in caplog.text
