"""Refresh one bot-owned status message, with restart-safe identity."""
from __future__ import annotations

import asyncio
from datetime import datetime

import discord

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_status_messages (
    channel_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, updated_at TEXT NOT NULL)"""


class StatusPublisher:
    def __init__(self, store) -> None:
        self.store = store
        self.messages: dict[int, int | None] = {}

    def _read(self, channel_id: int) -> int | None:
        with self.store.connect() as conn:
            conn.execute(SCHEMA)
            row = conn.execute("SELECT message_id FROM rival_status_messages WHERE channel_id=?",
                               (str(channel_id),)).fetchone()
        return int(row['message_id']) if row else None

    def _save(self, channel_id: int, message_id: int, now: datetime) -> None:
        with self.store.connect() as conn:
            conn.execute(SCHEMA)
            conn.execute("""INSERT INTO rival_status_messages VALUES (?,?,?)
                ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id,
                updated_at=excluded.updated_at""",
                         (str(channel_id), str(message_id), now.isoformat()))

    async def refresh(self, channel, embed: discord.Embed, *, now: datetime) -> None:
        if channel.id not in self.messages:
            self.messages[channel.id] = await asyncio.to_thread(self._read, channel.id)
        message_id = self.messages[channel.id]
        if message_id is not None:
            try:
                await channel.get_partial_message(message_id).edit(embed=embed)
            except discord.NotFound as exc:
                if exc.code != 10008:
                    raise
                message_id = None
        if message_id is None:
            message = await channel.send(embed=embed)
            message_id = message.id
            self.messages[channel.id] = message_id
        # Save after editing too: if a prior save failed, the cached message
        # is retried without sending another card. Forbidden never recreates.
        await asyncio.to_thread(self._save, channel.id, message_id, now)
