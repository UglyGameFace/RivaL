"""RivaL is a private bot: only its owner may use commands, buttons or modals.

Owners come from ``RIVAL_OWNER_USER_IDS`` when set, otherwise from Discord's
record of the application owner (or its team members). If owners cannot be
resolved, every interaction is refused (fail closed) and retried next time.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ui.modal import Modal
from discord.ui.view import BaseView

_LOG = logging.getLogger(__name__)
_MARKER = "_rival_owner_gate"
PRIVATE_NOTICE = "🔒 RivaL is a private bot."


def parse_owner_ids(value: str) -> frozenset[int]:
    ids = set()
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not 15 <= len(part) <= 21:
            raise ValueError("RIVAL_OWNER_USER_IDS must be Discord user IDs")
        ids.add(int(part))
    return frozenset(ids)


class OwnerGate:
    def __init__(self, client: discord.Client, configured: str = "") -> None:
        self._client = client
        self._configured = parse_owner_ids(configured)
        self._owners: frozenset[int] | None = self._configured or None
        self._lock = asyncio.Lock()

    async def owner_ids(self) -> frozenset[int]:
        if self._owners is not None:
            return self._owners
        async with self._lock:
            if self._owners is not None:
                return self._owners
            try:
                info = await asyncio.wait_for(self._client.application_info(), timeout=5)
            except (discord.HTTPException, TimeoutError):
                _LOG.warning("RivaL owner lookup failed; refusing interactions for now")
                return frozenset()
            team = getattr(info, "team", None)
            if team is not None and team.members:
                owners = frozenset(member.id for member in team.members)
            else:
                owners = frozenset({info.owner.id})
            self._owners = owners
            _LOG.info("RivaL private access limited to %d owner account(s)", len(owners))
            return owners

    async def allows(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id in await self.owner_ids()


_gate: OwnerGate | None = None


async def _refuse(interaction: discord.Interaction) -> None:
    _LOG.info("RivaL refused non-owner interaction user_id=%s", interaction.user.id)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(PRIVATE_NOTICE, ephemeral=True)
    except discord.HTTPException:
        pass


def _wrap_dispatch(cls: type, interaction_index: int) -> None:
    """interaction_index: position of the Interaction after ``self``
    (View: ``(item, interaction)`` -> 1; Modal: ``(interaction, ...)`` -> 0)."""
    original = cls._scheduled_task
    if getattr(original, _MARKER, False):
        return

    async def scheduled(self, *args, **kwargs):
        interaction = args[interaction_index]
        if _gate is not None and not await _gate.allows(interaction):
            await _refuse(interaction)
            return None
        return await original(self, *args, **kwargs)

    scheduled.__name__ = original.__name__
    scheduled.__qualname__ = original.__qualname__
    setattr(scheduled, _MARKER, True)
    cls._scheduled_task = scheduled


def install_owner_gate(tree: app_commands.CommandTree, gate: OwnerGate) -> None:
    """Gate slash commands (public tree check) and every view/modal dispatch
    (discord.py 2.7.1 private ``_scheduled_task``; a test pins its shape)."""
    global _gate
    _gate = gate
    check = tree.interaction_check
    if not getattr(check, _MARKER, False):

        async def interaction_check(interaction: discord.Interaction, /) -> bool:
            if not await gate.allows(interaction):
                await _refuse(interaction)
                return False
            return await check(interaction)

        setattr(interaction_check, _MARKER, True)
        tree.interaction_check = interaction_check  # type: ignore[method-assign]
    _wrap_dispatch(BaseView, 1)
    _wrap_dispatch(Modal, 0)
