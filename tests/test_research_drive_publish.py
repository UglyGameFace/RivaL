from __future__ import annotations

import json
from pathlib import Path

import pytest

from parlay_bot.archive.models import DriveObject
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.publish import prepare, publish


class FakeDrive:
    def __init__(self):
        self.calls = []

    async def ensure_history_file(self, *, artifact, batch_id, mime_type):
        self.calls.append(("history", batch_id, artifact.sha256, mime_type))
        return DriveObject("private-history", artifact.file_name, artifact.size_bytes,
                           artifact.md5, ("history-folder",))

    async def ensure_manifest_file(self, *, artifact, batch_id):
        self.calls.append(("manifest", batch_id, artifact.sha256))
        return DriveObject("private-manifest", artifact.file_name, artifact.size_bytes,
                           artifact.md5, ("manifests-folder",))


def fixture(tmp_path):
    result = _persist(
        source="football_data", key="E0-2526",
        records=[{"price": 2.25, "result": "H"}],
        raw_sha="f" * 64,
        url="https://www.football-data.co.uk/mmz4281/2526/E0.csv",
        out_dir=tmp_path,
    )
    return result.manifest_path


@pytest.mark.asyncio
async def test_private_drive_promotion_preserves_order_and_source_provenance(tmp_path):
    manifest = fixture(tmp_path)
    item = prepare(manifest)
    again = prepare(manifest)
    assert again.batch_id == item.batch_id
    drive = FakeDrive()
    result = await publish(drive, item)
    assert result["warehouse_promoted"] is True
    assert result["live_predictions_modified"] is False
    assert result["history_id"] == "private-history"
    assert result["manifest_id"] == "private-manifest"
    assert [call[0] for call in drive.calls] == ["history", "manifest"]
    assert drive.calls[0][-1] == "application/x-ndjson"


def test_publication_fails_closed_on_tampered_manifest_or_data(tmp_path):
    manifest = fixture(tmp_path)
    item = prepare(manifest)
    item.history.path.write_text(item.history.path.read_text() + "{}\n")
    with pytest.raises(HistoryImportError):
        prepare(manifest)
    info = json.loads(manifest.read_text())
    info["file"] = "../../anything.ndjson"
    manifest.write_text(json.dumps(info))
    with pytest.raises(HistoryImportError):
        prepare(manifest)


def test_publication_rejects_forecast_disguised_as_research(tmp_path):
    manifest = fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["prospective_forecast"] = True
    manifest.write_text(json.dumps(payload))
    with pytest.raises(HistoryImportError):
        prepare(manifest)


def test_source_file_name_contract(tmp_path):
    manifest = fixture(tmp_path)
    item = prepare(manifest)
    assert item.history.file_name.endswith(".ndjson")
    assert item.manifest.file_name.endswith(".manifest.json")
    assert item.manifest.path == Path(manifest).resolve()
