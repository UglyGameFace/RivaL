"""Original Gate hourly depth provenance: no synthetic L2 book or future label."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import gate_hourly_l2_archive as l2
from parlay_bot.research.free_history import HistoryImportError

HOUR = "2023042505"
CLOCK = datetime(2026, 11, 2, tzinfo=UTC)


def test_official_hourly_source_url_is_exact_and_utc():
    assert l2.source_url(
        asset="BTC", family="orderbooks", hour=HOUR, now=CLOCK,
    ) == "https://download.gatedata.org/futures_usdt/orderbooks/202304/BTC_USDT-2023042505.csv.gz"
    assert l2.source_url(
        asset="BTC", family="orderbooks_slice", hour=HOUR, now=CLOCK,
    ) == "https://download.gatedata.org/futures_usdt/orderbooks_slice/202304/BTC_USDT-2023042505.gz"
    for asset, family, hour in (
        ("DOGE", "orderbooks", HOUR),
        ("BTC", "bad_path", HOUR),
        ("BTC", "orderbooks", "2021070100"),
        ("BTC", "orderbooks", "2026120500"),
        ("BTC", "orderbooks", "2023130100"),
        ("BTC", "orderbooks", "2023-04-25"),
    ):
        with pytest.raises(HistoryImportError):
            l2.source_url(asset=asset, family=family, hour=hour, now=CLOCK)


def test_original_gzip_payload_is_bounded_and_not_fake_l2(monkeypatch):
    source = gzip.compress(b"timestamp,side,action,price,size\n123,bid,set,100,3\n")
    result = l2.inspect_gzip(source)
    assert result["gzip_integrity_verified"]
    assert result["newlines_in_original"] == 2
    assert result["linked_book_replay_verified"] is False
    assert result["l2_event_schema_verified"] is False
    for bad in (b"", b"not-gzip", source[:12]):
        with pytest.raises(HistoryImportError):
            l2.inspect_gzip(bad)
    monkeypatch.setattr(l2, "MAX_EXPANDED", 10)
    with pytest.raises(HistoryImportError, match="decompression cap"):
        l2.inspect_gzip(source)


def test_download_preserves_exact_bytes_and_rejects_provider_failure():
    original = gzip.compress(b"public original Gate source\n")
    urls = []

    def reply(request):
        urls.append(str(request.url))
        return httpx.Response(200, content=original)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        raw, metadata = l2.download(
            client, asset="BTC", family="orderbooks_slice",
            hour=HOUR, now=CLOCK,
        )
    assert raw == original
    assert metadata["original_source_sha256"] == hashlib.sha256(original).hexdigest()
    assert urls == [l2.source_url(
        asset="BTC", family="orderbooks_slice", hour=HOUR, now=CLOCK,
    )]
    with (httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404)
    )) as client, pytest.raises(HistoryImportError, match="HTTP 404")):
        l2.download(client, asset="BTC", family="orderbooks",
                    hour=HOUR, now=CLOCK)


@pytest.mark.asyncio
async def test_original_gate_l2_sha_uploaded_and_read_back_without_model_admission(
    tmp_path: Path,
):
    original = gzip.compress(b"one true original hourly snapshot\n")
    metadata = {
        "source_url": l2.source_url(
            asset="BTC", family="orderbooks_slice", hour=HOUR, now=CLOCK,
        ),
        "original_source_sha256": hashlib.sha256(original).hexdigest(),
        **l2.inspect_gzip(original),
    }

    class Drive:
        folders = SimpleNamespace(history="history", manifests="manifests")

        def __init__(self):
            self.files = []
            self.readbacks = []

        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            assert mime_type == "application/gzip"
            self.files.append(artifact)
            return SimpleNamespace(file_id=artifact.sha256)

        async def ensure_manifest_file(self, *, artifact, batch_id):
            self.files.append(artifact)
            return SimpleNamespace(file_id=artifact.sha256)

        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            assert file_id == artifact.sha256
            self.readbacks.append((parent_id, file_id))

    drive = Drive()
    result = await l2.store(
        drive, raw=original, evidence=metadata, asset="BTC",
        family="orderbooks_slice", hour=HOUR, root=tmp_path,
        now=CLOCK,
    )
    assert result["drive_sha256_readback_verified"]
    assert result["l2_hourly_complete"] is False
    assert result["model_training_eligible"] is False
    assert drive.files[0].path.read_bytes() == original
    manifest = json.loads(drive.files[1].path.read_text())
    assert manifest["original_sha256"] == metadata["original_source_sha256"]
    assert manifest["historical_orderbook_continuity_verified"] is False
    assert manifest["model_training_eligible"] is False
    assert len(drive.readbacks) == 2
    # Same bytes/source have identical deterministic source receipt, no edits.
    second = await l2.store(
        drive, raw=original, evidence=metadata, asset="BTC",
        family="orderbooks_slice", hour=HOUR, root=tmp_path,
        now=CLOCK,
    )
    assert second["original_sha256"] == result["original_sha256"]
    assert drive.files[3].path.read_bytes() == drive.files[1].path.read_bytes()
