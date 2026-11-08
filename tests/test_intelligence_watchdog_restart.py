"""Watchdog restarts a dead RivaL Intelligence review worker.

Regression test for the watchdog gap: ``_intelligence_task`` was absent
from the ``_health_loop`` restart tuple, so once ``_intelligence_loop``
exited (e.g. ``AttributeError`` on ``self.perp_collector.assets`` when the
collector is ``None``) it was never restarted and never heartbeated again.
The watchdog now restarts it when the LLM background opt-in is on, and
leaves a deliberately-disabled loop alone.
"""

import asyncio

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient


def _client(tmp_path, *, opt_in: bool) -> RivalDiscordClient:
    settings = Settings(
        _env_file=None,
        rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"),
        discord_token=None,
        rival_llm_background_opt_in=opt_in,
    )
    return RivalDiscordClient(settings)


async def _stop(task: asyncio.Task) -> None:
    """Cancel ``task`` until it actually finishes.

    On Python 3.11 ``asyncio.wait_for`` swallows a cancel that lands in the
    same loop iteration as its inner future completing, so a single
    ``cancel()`` can leave ``_health_loop`` alive in ``asyncio.sleep(120)``
    and hang the test (and the whole CI job). Re-cancel until it is done.
    """
    while not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=1)
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _run_one_watchdog_pass(client, *, stub, publish_seen):
    """Run ``_health_loop`` until one pass reaches ``publish``, then stop it."""
    published: list[dict] = []
    loop = asyncio.get_running_loop()

    def fake_publish(faults, now=None):
        # Runs in a worker thread (asyncio.to_thread): wake the loop safely.
        published.append(dict(faults))
        loop.call_soon_threadsafe(publish_seen.set)
        return []

    client.health_monitor.inspect = lambda now, **kwargs: {}
    client.health_monitor.publish = fake_publish

    async def already_ready() -> None:
        return None

    client.wait_until_ready = already_ready  # type: ignore[method-assign]
    client.is_closed = lambda: False  # type: ignore[method-assign]

    calls: list[str] = []

    async def fake_intelligence_loop() -> None:
        calls.append("rival-intelligence-review")
        stub.set()

    client._intelligence_loop = fake_intelligence_loop  # type: ignore[method-assign]

    async def already_done() -> None:
        return None

    old_task = asyncio.create_task(already_done())
    await old_task
    assert old_task.done()
    client._intelligence_task = old_task

    health = asyncio.create_task(client._health_loop())
    try:
        await asyncio.wait_for(publish_seen.wait(), timeout=10)
    finally:
        await _stop(health)
        current = client._intelligence_task
        if current is not None and not current.done():
            current.cancel()
            try:
                await current
            except asyncio.CancelledError:
                pass
    return old_task, calls, published


@pytest.mark.asyncio
async def test_watchdog_restarts_done_intelligence_task(tmp_path) -> None:
    client = _client(tmp_path, opt_in=True)
    restarted = asyncio.Event()
    publish_seen = asyncio.Event()
    try:
        old_task, calls, published = await _run_one_watchdog_pass(
            client, stub=restarted, publish_seen=publish_seen,
        )
        assert restarted.is_set(), "watchdog did not restart _intelligence_loop"
        assert calls == ["rival-intelligence-review"]
        assert client._intelligence_task is not old_task
        assert published, "watchdog pass never reached publish"
        assert published[0].get("worker:rival-intelligence-review") == (
            "background worker exited unexpectedly"
        )
    finally:
        await client.zip_resolver.aclose()


@pytest.mark.asyncio
async def test_watchdog_leaves_opted_out_intelligence_task_alone(tmp_path) -> None:
    client = _client(tmp_path, opt_in=False)
    restarted = asyncio.Event()
    publish_seen = asyncio.Event()
    try:
        old_task, calls, published = await _run_one_watchdog_pass(
            client, stub=restarted, publish_seen=publish_seen,
        )
        assert not restarted.is_set(), "watchdog restarted an opted-out loop"
        assert calls == []
        assert client._intelligence_task is old_task
        assert published, "watchdog pass never reached publish"
        assert "worker:rival-intelligence-review" not in published[0]
    finally:
        await client.zip_resolver.aclose()
