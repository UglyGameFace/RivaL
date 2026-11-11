"""Finish a slow screen instead of giving up on it.

Every RivaL screen used to wait a fixed 8-12 s for its render and then show
"took too long, press again". The render's database reads kept running in
their threads (Python cannot stop them), so the second press found them
done: the owner had to press Update two or three times on a slow database.

``patient`` waits ``first`` seconds as before. If the render is not done it
calls ``on_slow`` (show "still loading, this will update itself") and keeps
waiting on the SAME render, up to ``total`` seconds, instead of starting
over. The caller still decides whether the result is current (a newer
press wins).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

_LOG = logging.getLogger(__name__)

FIRST_WAIT = 8.0
TOTAL_WAIT = 45.0


def still_loading(title: str = "Still loading…") -> discord.Embed:
    return discord.Embed(
        title=f"⏳ {title}",
        description=("The database is slow right now. This screen will update "
                     "itself when the check finishes; no need to press again."),
        color=discord.Color.blurple(),
    )


async def patient(render: Callable[[], Awaitable[discord.Embed]], *,
                  on_slow: Callable[[], Awaitable[None]] | None = None,
                  first: float = FIRST_WAIT, total: float = TOTAL_WAIT) -> discord.Embed:
    """The render's embed; TimeoutError only after ``total`` seconds."""
    task = asyncio.ensure_future(render())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=first)
    except TimeoutError:
        pass
    except asyncio.CancelledError:   # a newer press replaced this screen
        task.cancel()
        raise
    if on_slow is not None:
        try:
            await on_slow()
        except Exception:  # noqa: BLE001 - the note is optional, the result is not
            _LOG.warning("Still-loading note could not be shown", exc_info=True)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, total - first))
    except TimeoutError:
        task.cancel()
        raise
    except asyncio.CancelledError:
        task.cancel()
        raise
