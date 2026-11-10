"""RIVAL-P/N/M: the 7-day challenger aggregate runs beside the watchdog pass.

The aggregate over the whole shadow ledger took 11-12 s on production. Run
inside the pass it hit the 12 s statement bound or the 15 s wait, and the
pass then published nothing ("health is unverified"). The pass now only
reads the verdict ``refresh_regressions`` stored; an unknown verdict keeps
open regressions open instead of closing and re-raising them.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import (
    REGRESSION_RECHECK,
    REGRESSION_STALE,
    RuntimeHealthMonitor,
)
from parlay_bot.storage import query_scope
from parlay_bot.storage.hot import SQLiteHotStore
from parlay_bot.storage.query_scope import (
    BACKGROUND_STATEMENT_TIMEOUT_MS,
    WATCHDOG_STATEMENT_TIMEOUT_MS,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
KEY = "regression:dydx:BTC-USD:liq_v1"


def _ledger(tmp_path, name) -> SQLiteHotStore:
    """A store with one challenger losing to its incumbent over 60 settled calls."""
    store = SQLiteHotStore(tmp_path / name)
    store.initialize()
    market = PerpetualMarketStore(store)
    market.initialize()
    ForwardAccuracyMonitor(market).initialize()
    ShadowModelCompetition(store).initialize()
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
            slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
            math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
            math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
            model_review_ok INTEGER NOT NULL, auto_ai_ok INTEGER NOT NULL)""")
        for i in range(60):
            at = (NOW - timedelta(minutes=300 - 5 * i)).isoformat()
            conn.execute(
                """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                     horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                     probability_up,incumbent_up,baseline_up,training_count,training_sha256,
                     settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("dydx", "BTC-USD", "USD", "BTC", 5, "liq_v1", at, at, at,
                 .9, .5, .5, 10, "0" * 64, at, 0, .81, .25, .25))
    return store


def _watchdog(monitor, now):
    return monitor.inspect(now=now, learning_enabled=True, regressions_inline=False)


def _regressions(faults):
    """Only the challenger-score faults (the fixture has no learning cycles)."""
    return {key: text for key, text in faults.items() if key.startswith("regression")}


def test_watchdog_pass_reads_the_stored_verdict_and_never_runs_the_aggregate(tmp_path):
    store = _ledger(tmp_path, "stored.sqlite")
    try:
        monitor = RuntimeHealthMonitor(store)  # just started: not warm yet
        monitor.started_at = NOW - timedelta(minutes=6)
        assert monitor.regressions_due(NOW)
        # The losing challenger is in the ledger, but this pass does not score it.
        assert _regressions(_watchdog(monitor, NOW)) == {}
        assert monitor._regression_cache is None
        # The side refresh scores it; the next pass reports it.
        assert KEY in monitor.refresh_regressions(now=NOW)
        assert not monitor.regressions_due(NOW + timedelta(minutes=2))
        assert KEY in _watchdog(monitor, NOW + timedelta(minutes=2))
        assert monitor.regressions_due(NOW + REGRESSION_RECHECK)
    finally:
        store.close()


def test_an_unknown_verdict_keeps_open_regressions_open_across_a_restart(tmp_path):
    store = _ledger(tmp_path, "restart.sqlite")
    try:
        before = RuntimeHealthMonitor(store)
        before.started_at = NOW - timedelta(hours=2)
        before.refresh_regressions(now=NOW)
        opened = before.publish(_watchdog(before, NOW), now=NOW)
        detail = {key: text for state, key, text in opened if state == "OPEN"}[KEY]

        after = RuntimeHealthMonitor(store)  # the process restarted: no verdict yet
        restart = NOW + timedelta(minutes=3)
        after.started_at = restart - timedelta(seconds=20)
        faults = _watchdog(after, restart)
        # Still open with its last verified wording, and no stall claim this early.
        assert _regressions(faults) == {KEY: detail}
        # Publishing it is not a transition: no RESOLVED now, no re-OPEN later.
        assert not any(key == KEY for _, key, _ in after.publish(faults, now=restart))
    finally:
        store.close()


def test_a_check_that_never_completes_is_reported_not_silently_cleared(tmp_path):
    store = _ledger(tmp_path, "stalled.sqlite")
    try:
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=1)  # warm, verdict never arrived
        assert set(_regressions(_watchdog(monitor, NOW))) == {"regression_check_stalled"}
        # A verdict older than three missed rechecks is unknown again.
        monitor.refresh_regressions(now=NOW)
        fresh = _watchdog(monitor, NOW + REGRESSION_STALE - timedelta(seconds=1))
        assert set(_regressions(fresh)) == {KEY}
        monitor.publish(_watchdog(monitor, NOW), now=NOW)
        late = _watchdog(monitor, NOW + REGRESSION_STALE + timedelta(seconds=1))
        assert set(_regressions(late)) == {KEY, "regression_check_stalled"}
        # A completed check clears the stall on the next pass.
        monitor.refresh_regressions(now=NOW + REGRESSION_STALE + timedelta(minutes=1))
        # (By then the fixture's challenger last issued over 30 minutes ago, so
        # the fresh verdict no longer counts it as in use.)
        cleared = _watchdog(monitor, NOW + REGRESSION_STALE + timedelta(minutes=2))
        assert "regression_check_stalled" not in cleared
    finally:
        store.close()


def _client(tmp_path) -> RivalDiscordClient:
    return RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"), discord_token=None,
        rival_llm_background_opt_in=False,
    ))


async def _stop(task: asyncio.Task) -> None:
    while not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=1)
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a_slow_aggregate_no_longer_costs_the_watchdog_its_pass(tmp_path):
    """The pass publishes while the aggregate is still running beside it."""
    client = _client(tmp_path)
    loop = asyncio.get_running_loop()
    published = asyncio.Event()
    refresh_started = asyncio.Event()
    release_refresh = threading.Event()
    seen: dict[str, list] = {"inspect": [], "refresh": [], "publish": []}

    def fake_inspect(now, **kwargs):
        seen["inspect"].append(
            (query_scope.current_statement_timeout_ms(), kwargs["regressions_inline"]))
        return {}

    def slow_refresh(now=None):
        seen["refresh"].append(query_scope.current_statement_timeout_ms())
        loop.call_soon_threadsafe(refresh_started.set)
        release_refresh.wait(timeout=10)  # still running when the pass publishes
        return {}

    def fake_publish(faults, now=None):
        seen["publish"].append(release_refresh.is_set())
        loop.call_soon_threadsafe(published.set)
        return []

    client.health_monitor.inspect = fake_inspect  # type: ignore[method-assign]
    client.health_monitor.refresh_regressions = slow_refresh  # type: ignore[method-assign]
    client.health_monitor.publish = fake_publish  # type: ignore[method-assign]
    client.autonomous = SimpleNamespace()
    client._boot_at = datetime.now(UTC) - timedelta(hours=1)

    async def already_ready() -> None:
        return None

    client.wait_until_ready = already_ready  # type: ignore[method-assign]
    client.is_closed = lambda: False  # type: ignore[method-assign]

    health = asyncio.create_task(client._health_loop())
    try:
        await asyncio.wait_for(published.wait(), timeout=10)
        await asyncio.wait_for(refresh_started.wait(), timeout=5)
    finally:
        release_refresh.set()
        await _stop(health)
        if client._regression_task is not None:
            await asyncio.wait_for(client._regression_task, timeout=5)
        await client.zip_resolver.aclose()

    # The pass kept its 12 s bound and did not run the aggregate itself...
    assert seen["inspect"] == [(WATCHDOG_STATEMENT_TIMEOUT_MS, False)]
    # ...published before the aggregate finished...
    assert seen["publish"] == [False]
    # ...and the aggregate ran once, under the 60 s background bound.
    assert seen["refresh"] == [BACKGROUND_STATEMENT_TIMEOUT_MS]


@pytest.mark.asyncio
async def test_the_aggregate_waits_out_the_startup_burst(tmp_path):
    client = _client(tmp_path)
    loop = asyncio.get_running_loop()
    published = asyncio.Event()
    refreshed: list[int] = []

    client.health_monitor.inspect = lambda now, **kwargs: {}  # type: ignore[method-assign]
    client.health_monitor.refresh_regressions = (  # type: ignore[method-assign]
        lambda now=None: refreshed.append(1) or {})

    def fake_publish(faults, now=None):
        loop.call_soon_threadsafe(published.set)
        return []

    client.health_monitor.publish = fake_publish  # type: ignore[method-assign]
    client.autonomous = SimpleNamespace()
    client._boot_at = datetime.now(UTC)  # just booted

    async def already_ready() -> None:
        return None

    client.wait_until_ready = already_ready  # type: ignore[method-assign]
    client.is_closed = lambda: False  # type: ignore[method-assign]

    health = asyncio.create_task(client._health_loop())
    try:
        await asyncio.wait_for(published.wait(), timeout=10)
    finally:
        await _stop(health)
        await client.zip_resolver.aclose()
    assert refreshed == [] and client._regression_task is None
