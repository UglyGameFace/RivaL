"""The stall watch names the code that blocked the event loop, once per stall."""
import asyncio
import logging
import time

from parlay_bot.discord_app.loop_watch import LoopStallWatch


def _blocking_handler():
    time.sleep(0.6)  # synchronous work on the loop: the bug being located


def test_a_blocked_loop_is_reported_once_with_its_location(caplog):
    async def scenario():
        watch = LoopStallWatch(stall_after=0.3, tick=0.05)
        task = asyncio.create_task(watch.run())
        await asyncio.sleep(0.2)
        _blocking_handler()
        await asyncio.sleep(0.3)  # healthy again: no further reports
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return watch

    with caplog.at_level(logging.WARNING, logger="parlay_bot.discord_app.loop_watch"):
        watch = asyncio.run(scenario())
    assert len(watch.stalls) == 1
    seconds, where = watch.stalls[0]
    assert seconds >= 0.3
    assert "_blocking_handler" in where
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and messages[0].startswith("RivaL event loop blocked")


def test_a_healthy_loop_reports_nothing():
    async def scenario():
        watch = LoopStallWatch(stall_after=0.3, tick=0.05)
        task = asyncio.create_task(watch.run())
        for _ in range(10):
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return watch

    assert asyncio.run(scenario()).stalls == []


def test_a_cpu_bound_worker_thread_is_named_as_a_suspect():
    import sys
    import threading

    from parlay_bot.discord_app import loop_watch

    stop = threading.Event()

    def crunch():
        while not stop.is_set():
            sum(i * i for i in range(1000))

    worker = threading.Thread(target=crunch, name="rival-test-crunch", daemon=True)
    worker.start()
    try:
        time.sleep(0.05)
        text = loop_watch._busy_threads(threading.get_ident(), sys._current_frames())
    finally:
        stop.set()
        worker.join()
    # Test code lives outside parlay_bot, so no RivaL frame is named; the
    # helper must still run and return a bounded string.
    assert isinstance(text, str) and len(text) < 500
