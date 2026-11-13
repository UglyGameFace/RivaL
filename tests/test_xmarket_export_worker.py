"""Exercise the real bot worker, Drive publisher and verified training restore."""
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.archive.drive import (
    DriveArchiveError,
    DriveArchiveFolders,
    GoogleDriveArchiveClient,
)
from parlay_bot.perpetuals.xmarket_inputs import BUNDLE_PREFIX, XmarketInputs
from parlay_bot.research import xmarket_history
from tests.xmarket_fixtures import point


class ReportWarehouse:
    """A transport-level fixture; production Drive validation stays in use."""

    def __init__(self):
        self.files = {}
        self.content = {}
        self.requests = []

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "test", "expires_in": 3600})
        if path.startswith("/drive/v3/files/"):
            identity = path.rsplit("/", 1)[-1]
            if identity in ("root", "history", "manifests", "staging", "models", "reports"):
                return httpx.Response(200, json={"id": identity, "name": identity,
                    "mimeType": "application/vnd.google-apps.folder", "trashed": False,
                    "parents": [] if identity == "root" else ["root"]})
            item = self.files[identity]
            if request.method == "PATCH":
                assert request.url.params["addParents"] == "reports"
                assert request.url.params["removeParents"] == "staging"
                item["parents"] = ["reports"]
            if request.url.params.get("alt") == "media":
                return httpx.Response(200, content=self.content[identity])
            return httpx.Response(200, json=item)
        if path == "/drive/v3/files":
            query = request.url.params["q"]
            parent = re.search(r"'([^']+)' in parents", query).group(1)
            batch = re.search(r"key='rival_batch_id' and value='([^']+)'", query)
            name = re.search(r"name contains '([^']+)'", query)
            files = [f for f in self.files.values() if parent in f["parents"]
                     and (not batch or f["appProperties"]["rival_batch_id"] == batch.group(1))
                     and (not name or name.group(1) in f["name"])]
            return httpx.Response(200, json={"files": files})
        if path == "/upload/drive/v3/files":
            identity = "file-" + str(len(self.files) + 1)
            item = {"id": identity, **json.loads(request.content)}
            assert item["parents"] == ["staging"]
            assert item["appProperties"]["rival_kind"] == "report"
            self.files[identity] = item
            return httpx.Response(200, headers={"Location": "https://upload.example/" + identity})
        if request.url.host == "upload.example":
            identity = path.rsplit("/", 1)[-1]
            self.content[identity] = request.content
            self.files[identity].update(size=str(len(request.content)),
                md5Checksum=hashlib.md5(request.content, usedforsecurity=False).hexdigest())
            return httpx.Response(200, json=self.files[identity])
        raise AssertionError((request.method, str(request.url)))


def drive(http, *, reports):
    return GoogleDriveArchiveClient(client_id="client", client_secret="secret",
        refresh_token="refresh", client=http, folders=DriveArchiveFolders(
            root="root", history="history", manifests="manifests", staging="staging",
            models="models", reports=reports))


@pytest.mark.asyncio
async def test_completed_days_reach_reports_and_restore_through_the_real_worker(
        learning_store, monkeypatch, tmp_path, caplog):
    from parlay_bot.discord_app import bot
    from parlay_bot.perpetuals import records_backup

    caplog.set_level("INFO")
    clock = datetime.now(UTC)
    previous = (clock - timedelta(days=2)).replace(hour=12, minute=5, second=0, microsecond=0)
    inputs = XmarketInputs(learning_store)
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        inputs.record([point(source, previous, asset=asset) for source in ("polymarket", "kalshi")])
    warehouse = ReportWarehouse()
    async with httpx.AsyncClient(transport=httpx.MockTransport(warehouse.handle)) as http:
        reader = drive(http, reports=None)
        publisher = drive(http, reports="reports")
        # Reproduce the actual old route: it fails before contacting Drive.
        with pytest.raises(DriveArchiveError, match="output folder not configured"):
            await inputs.publish_days(reader, now=clock)
        assert warehouse.requests == []

        backups = []
        class Backup:
            def __init__(self, store, client, workdir):
                assert client is reader
            async def run_once(self):
                backups.append(True)
                raise RuntimeError("unrelated backup failure")

        client = SimpleNamespace(store=learning_store, research_drive=reader,
                                 research_loop_drive=publisher, closed=False)
        async def ready():
            pass
        async def advance(seconds):
            if seconds == 3600:
                client.closed = True
        client.wait_until_ready = ready
        client.is_closed = lambda: client.closed
        monkeypatch.setattr(records_backup, "RecordsBackup", Backup)
        monkeypatch.setattr(bot.asyncio, "sleep", advance)
        monkeypatch.setattr(bot.host_memory, "snapshot", lambda: SimpleNamespace(pressure=False))
        await bot.RivalDiscordClient._records_loop(client)
        assert backups == [True]
        assert len(warehouse.files) == 4
        with learning_store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM rival_xmarket_exports").fetchone()["n"] == 4
        assert "XMarket completed-day source exports published=4" in caplog.text

        # Independently restore the published bytes with the trainer's real
        # folder/hash verification. Native-history acquisition is outside this
        # fixture, so overlap must remain unverified, never a manufactured fit.
        monkeypatch.setattr(xmarket_history, "backfill",
                            lambda *a, **kw: ([], [], clock, []))
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            restored = await xmarket_history.weekly_inputs(publisher, asset=asset,
                through=clock, cache=tmp_path / asset, max_windows=0)
            assert len(restored["samples"]) == 2
            assert {p["asset"] for p in restored["samples"]} == {asset}
            assert {p["origin"] for p in restored["samples"]} == {"live"}
            assert len(restored["receipts"]) == 1
            assert all(v["status"] == "unverified" for v in restored["verification"].values())
        assert all(f["parents"] == ["reports"] and f["name"].startswith(BUNDLE_PREFIX)
                   for f in warehouse.files.values())
        # A later normal pass sees saved export receipts and uploads no copies.
        client.closed = False
        await bot.RivalDiscordClient._records_loop(client)
        assert len(warehouse.files) == 4


@pytest.mark.asyncio
async def test_missing_reports_configuration_keeps_existing_records_backup(monkeypatch):
    from parlay_bot.discord_app import bot
    from parlay_bot.perpetuals import records_backup

    completed = []
    class Backup:
        def __init__(self, *args):
            pass
        async def run_once(self):
            completed.append(True)
    client = SimpleNamespace(store=object(), research_drive=object(),
                             research_loop_drive=None, closed=False)
    async def ready():
        pass
    async def advance(seconds):
        if seconds == 3600:
            client.closed = True
    client.wait_until_ready = ready
    client.is_closed = lambda: client.closed
    monkeypatch.setattr(records_backup, "RecordsBackup", Backup)
    monkeypatch.setattr(bot.asyncio, "sleep", advance)
    monkeypatch.setattr(bot.host_memory, "snapshot", lambda: SimpleNamespace(pressure=False))
    await bot.RivalDiscordClient._records_loop(client)
    assert completed == [True]
