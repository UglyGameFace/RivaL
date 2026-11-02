"""The existing hosted candidate worker must recover without Discord interaction."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import parlay_bot.discord_app.bot as bot_module
from parlay_bot.discord_app.bot import RivalDiscordClient


@pytest.mark.asyncio
async def test_optional_model_import_recovers_startup_pool_failure(monkeypatch):
    class Registry:
        def __init__(self):
            self.initializations = 0
            self.imported = []

        def initialize(self):
            self.initializations += 1
            if self.initializations == 1:
                raise RuntimeError("temporary PostgreSQL pool pressure")

        def import_document(self, file_id, payload):
            self.imported.append((file_id, payload))
            return 1

        def inventory_receipt(self):
            return {"total": len(self.imported), "compatible": 1, "passed": 0, "missing_artifacts": 0}

    class Drive:
        async def read_verified_research_candidates(self, **kwargs):
            assert kwargs["limit"] == 20
            return [("verifiedDriveFile", b"verified")], None

    class FakeClient:
        def __init__(self):
            self.offline_registry = Registry()
            self.research_drive = Drive()
            self._candidate_inventory_cursor = None
            self.candidate_sync_checked_at = None
            self.candidate_sync_status = "pending_first_verified_drive_read"
            self.closed = False

        async def wait_until_ready(self):
            return None

        def is_closed(self):
            return self.closed

    client = FakeClient()
    delays = []

    async def advance(delay):
        delays.append(delay)
        # Stop the fake perpetual worker after its first successful import.
        # Do NOT tie teardown to a legacy sleep duration: that previously
        # looped forever when the import cadence was reduced from six hours.
        if len(delays) == 2:
            client.closed = True
        elif len(delays) > 2:
            raise AssertionError("candidate sync did not stop after first import")

    monkeypatch.setattr(
        bot_module.host_memory, "snapshot",
        lambda: SimpleNamespace(pressure=False),
    )
    monkeypatch.setattr(bot_module.asyncio, "sleep", advance)

    # Exercise the real worker method without connecting to Discord/Drive.
    await RivalDiscordClient._candidate_import_loop(client)

    assert client.offline_registry.initializations >= 2
    assert client.offline_registry.imported == [
        ("verifiedDriveFile", b"verified"),
    ]
    assert delays == [60, 3600]  # startup retry, then verified idle rescan
    assert client.candidate_sync_status == "verified_drive_read_1_db_1_screened_0"
    assert client.candidate_sync_checked_at is not None
