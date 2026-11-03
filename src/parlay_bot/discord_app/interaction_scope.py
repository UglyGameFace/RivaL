"""Mark every Discord interaction task so its database queries are limited.

discord.py runs each slash command, button/select tap and modal submit in
its own asyncio task. Marking the task (a context variable) applies the
interactive PostgreSQL statement limit to all queries it makes, including
those in ``asyncio.to_thread`` calls and tasks it spawns. Background
workers are separate tasks and stay unlimited.
"""
from __future__ import annotations

import functools
import logging

import discord
from discord import app_commands
from discord.ui.modal import Modal
from discord.ui.view import BaseView

from parlay_bot.storage.query_scope import mark_interactive

_LOG = logging.getLogger(__name__)
_INSTALLED = "_rival_interaction_scope"


def _wrap(cls: type) -> None:
    original = cls._scheduled_task
    if getattr(original, _INSTALLED, False):
        return

    @functools.wraps(original)
    async def scheduled(self, *args, **kwargs):
        mark_interactive()
        return await original(self, *args, **kwargs)

    setattr(scheduled, _INSTALLED, True)
    cls._scheduled_task = scheduled


def install_interaction_scope(tree: app_commands.CommandTree) -> None:
    """Mark slash commands, view taps and modal submits as interactive.

    The tree check runs inside each command's own task (public API). View
    and modal dispatch use discord.py's private ``_scheduled_task`` (pinned
    discord.py 2.7.1); tests assert it exists so an upgrade that renames it
    fails CI instead of silently dropping the limits.
    """
    check = tree.interaction_check
    if not getattr(check, _INSTALLED, False):

        @functools.wraps(check)
        async def interaction_check(interaction: discord.Interaction, /) -> bool:
            mark_interactive()
            return await check(interaction)

        setattr(interaction_check, _INSTALLED, True)
        tree.interaction_check = interaction_check  # type: ignore[method-assign]
    for cls in (BaseView, Modal):
        _wrap(cls)
    _LOG.info("RivaL interactive query limits installed")
