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

        def import_document(self, file_id, payload, *, source_manifests=None):
            # The listing is unavailable for this fake Drive, so the worker
            # must import labelled not_checked -- never silently verified.
            assert source_manifests is None
            self.imported.append((file_id, payload))
            return 1

        def fully_imported_file_ids(self):
            return frozenset({"alreadyImported"})

        def inventory_receipt(self):
            return {"total": len(self.imported), "compatible": 1, "passed": 0, "missing_artifacts": 0}

    class Drive:
        async def read_verified_research_candidates(self, **kwargs):
            assert kwargs["limit"] == 20
            assert kwargs["known_ids"] == frozenset({"alreadyImported"})
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

        def _pooled_model_set(self):
            return None

        def _archive_model_set(self):
            return None

        def _gbm_model_set(self):
            return None

        def _regime2_model_set(self):
            return None

        def _xmarket_model_set(self):
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


@pytest.mark.asyncio
async def test_xmarket_published_fit_is_imported_and_issued_by_actual_worker(
    learning_store, monkeypatch,
):
    """A verified Drive fit must pass through the host worker before shadow issuance.

    A green trainer alone never proves this handoff. Exercise the actual worker
    against the real runtime model registry and shadow ledger on both DB backends.
    """
    import json
    from datetime import timedelta

    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.perpetuals.xmarket_model import XmarketModelSet, xmarket_calls
    from tests.xmarket_fixtures import NOW, valid_doc

    doc = valid_doc(venues=(), now=NOW)
    payload = json.dumps(doc, sort_keys=True).encode()
    models = XmarketModelSet(learning_store)

    class Registry:
        def initialize(self):
            pass

        def fully_imported_file_ids(self):
            return frozenset()

        def inventory_receipt(self):
            return {"total": 0, "compatible": 0, "passed": 0, "missing_artifacts": 0}

    class VerifiedDrive:
        async def read_verified_research_candidates(self, **kwargs):
            assert kwargs["limit"] == 20
            assert kwargs["known_ids"] == frozenset()
            return [("verifiedXmarketModelFile", payload)], None

    class Client:
        offline_registry = Registry()
        research_drive = VerifiedDrive()
        _candidate_inventory_cursor = None
        candidate_sync_checked_at = None
        candidate_sync_status = "pending"
        closed = False

        async def wait_until_ready(self):
            pass

        def is_closed(self):
            return self.closed

        def _pooled_model_set(self):
            return None

        def _archive_model_set(self):
            return None

        def _gbm_model_set(self):
            return None

        def _regime2_model_set(self):
            return None

        def _xmarket_model_set(self):
            return models

    client = Client()
    waits = []

    async def stop_after_import(delay):
        waits.append(delay)
        client.closed = True

    monkeypatch.setattr(
        bot_module.host_memory, "snapshot",
        lambda: SimpleNamespace(pressure=False),
    )
    monkeypatch.setattr(bot_module.asyncio, "sleep", stop_after_import)

    await RivalDiscordClient._candidate_import_loop(client)
    assert waits == [3600]
    assert models.imported_file_ids() == frozenset({"verifiedXmarketModelFile"})
    assert models.import_document("verifiedXmarketModelFile", payload) == 0

    calls = xmarket_calls(
        models, asset="BTC", horizon=5,
        vector=([0.0] * doc["input_count"], {"source": "prospective"}),
        now=NOW,
    )
    assert len(calls) == 1
    assert calls[0]["family"] == "xmarket_v1"

    competition = ShadowModelCompetition(learning_store)
    competition.initialize()
    with learning_store.connect() as conn:
        saved = competition.issue(
            conn, samples=[], features=(0.25,) * 10, now=NOW,
            venue="hyperliquid", contract="BTC", quote="USDC",
            asset="BTC", horizon=5,
            reference_opened_at=NOW - timedelta(minutes=5),
            target_opened_at=NOW, issued_at=NOW,
            incumbent_up=0.6, baseline_up=0.5, challengers=calls,
        )
        rows = conn.execute(
            "SELECT family,training_sha256 FROM rival_model_shadow_forecasts "
            "WHERE asset='BTC' AND horizon_minutes=5 AND family='xmarket_v1'"
        ).fetchall()
    assert saved == 1
    assert len(rows) == 1
    assert rows[0]["training_sha256"] == calls[0]["model_sha256"]
