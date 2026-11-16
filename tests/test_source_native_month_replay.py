"""Duplicate native snapshots: exact replays only, divergent versions fail closed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.source_native import (
    SOURCE,
    NativeMonthConflictError,
    restore_verified_source_months,
)


def _bytes(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


class _Drive:
    DRIVE_URL = "https://www.googleapis.com/drive/v3"

    def __init__(self, *, changed_manifest=False, corrupt_replay=False):
        self.folders = SimpleNamespace(manifests="manifests", history="history")
        self.data = b'{"closed_at":"2026-09-01T00:05:00+00:00"}\n'
        file = "okx-BTC-USDT-SWAP-USDT-5m-2026-09--0000000000000001.ndjson"
        doc = {
            "file": file, "source": SOURCE, "venue": "okx",
            "contract": "BTC-USDT-SWAP", "quote": "USDT",
            "interval_minutes": 5, "source_integrity": "postgres-first-observed-at-v1",
            "prospective_forecast": False, "live_trading_authorized": False,
            "data_sha256": hashlib.sha256(self.data).hexdigest(),
        }
        first = _bytes(doc)
        second = _bytes({**doc, "different_provenance": "new"})
        if not changed_manifest:
            second = first
        self.blobs = {"m1": first, "m2": first if corrupt_replay else second,
                      "h1": self.data}
        self.manifests = [
            {"id": key, "name": file.removesuffix(".ndjson")+".manifest.json",
             "size": str(len(blob)), "parents": ["manifests"],
             "appProperties": {"rival_kind": "manifest",
                               "rival_sha256": hashlib.sha256(blob).hexdigest()}}
            for key, blob in (("m1", first), ("m2", second))
        ]
        if corrupt_replay:
            # The second metadata authenticates different manifest bytes;
            # the downloaded copy is tampered. Must fail before replay skip.
            self.manifests[1]["appProperties"]["rival_sha256"] = hashlib.sha256(second).hexdigest()
        self.calls = []
        self._client = self

    async def _verify_folder_boundary(self):
        return None

    @staticmethod
    def _escape_query(value):
        return value

    async def _headers(self):
        return {}

    def _object(self, record):
        return SimpleNamespace(
            file_id=record["id"], parents=record["parents"],
            size_bytes=int(record["size"]), name=record["name"])

    async def _find_batch_object(self, *, parent_id, batch_id, kind):
        assert (parent_id, kind) == ("history", "history")
        assert batch_id == "native-" + hashlib.sha256(self.data).hexdigest()[:24]
        file = self.manifests[0]["name"].removesuffix(".manifest.json") + ".ndjson"
        return SimpleNamespace(file_id="h1", name=file, size_bytes=len(self.data))

    async def get(self, url, *, params, headers, follow_redirects=False):
        if params.get("alt") != "media":
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"files": self.manifests})
        key = url.rsplit("/", 1)[-1]
        self.calls.append(key)
        return SimpleNamespace(status_code=200, content=self.blobs[key])


@pytest.mark.asyncio
async def test_native_replay_same_manifest_sha_is_safe_and_idempotent(tmp_path):
    drive = _Drive()
    files = await restore_verified_source_months(
        drive, venue="okx", contract="BTC-USDT-SWAP",
        months=["2026-09"], root=tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes().startswith(b"{")
    assert drive.calls.count("m1") == drive.calls.count("m2") == 1
    assert drive.calls.count("h1") == 1
    assert len(list(Path(tmp_path).rglob("*.manifest.json"))) == 1


@pytest.mark.asyncio
async def test_native_replay_different_manifest_provenance_fails_closed(tmp_path):
    drive = _Drive(changed_manifest=True)
    with pytest.raises(NativeMonthConflictError,
                       match="conflicting native month snapshots") as caught:
        await restore_verified_source_months(
            drive, venue="okx", contract="BTC-USDT-SWAP",
            months=["2026-09"], root=tmp_path)
    evidence = caught.value.evidence
    assert (evidence["venue"], evidence["contract"], evidence["quote"],
            evidence["month"]) == ("okx", "BTC-USDT-SWAP", "USDT", "2026-09")
    assert evidence["schema"] == "rival-native-month-conflict-v1"
    assert evidence["resolution"] == "blocked_divergent_manifest_bytes"
    assert evidence["first"]["manifest_sha256"] != evidence["second"]["manifest_sha256"]
    assert evidence["first"]["claimed_history_sha256"] == hashlib.sha256(drive.data).hexdigest()
    assert evidence["second"]["claimed_history_sha256"] == hashlib.sha256(drive.data).hexdigest()
    assert evidence["verification"] == {
        "both_manifests_sha_verified": True,
        "first_history_readback_verified": True,
        "second_history_readback_verified": False,
    }
    assert "m1" not in json.dumps(evidence)
    assert "m2" not in json.dumps(evidence)
    assert "source_url" not in json.dumps(evidence)
    assert drive.calls.count("m2") == 1


@pytest.mark.asyncio
async def test_native_replay_requires_sha_verification_even_on_duplicate(tmp_path):
    drive = _Drive(changed_manifest=True, corrupt_replay=True)
    with pytest.raises(HistoryImportError, match="byte identity mismatch"):
        await restore_verified_source_months(
            drive, venue="okx", contract="BTC-USDT-SWAP",
            months=["2026-09"], root=tmp_path)
    assert drive.calls.count("m2") == 1


@pytest.mark.asyncio
async def test_archive_restore_retains_credential_free_conflict_evidence(tmp_path, monkeypatch):
    from parlay_bot.research import (
        archive_training,
        bitget_source_bridge,
        recorded_candles,
        source_native,
    )

    receipt = {
        "schema": "rival-native-month-conflict-v1",
        "venue": "okx", "contract": "BTC-USDT-SWAP",
        "quote": "USDT", "month": "2026-09",
        "first": {"manifest_sha256": "a" * 64},
        "second": {"manifest_sha256": "b" * 64},
        "resolution": "blocked_divergent_manifest_bytes",
    }

    async def bitget_restore(*args, **kwargs):
        return []

    async def source_restore(*args, venue, **kwargs):
        if venue == "okx":
            raise NativeMonthConflictError(receipt)
        return []

    async def recorded_restore(*args, **kwargs):
        return {"restored": []}

    monkeypatch.setattr(
        bitget_source_bridge, "restore_verified_bitget_market_months", bitget_restore,
    )
    monkeypatch.setattr(source_native, "restore_verified_source_months", source_restore)
    monkeypatch.setattr(recorded_candles, "restore_recorded_partitions", recorded_restore)
    report = await archive_training.restore_verified_months(
        asset="BTC", months=["2026-09"], root=tmp_path, drive=object(),
    )
    assert report["excluded"]["okx"]["source_conflict"] == receipt
    assert report["excluded"]["okx"]["reason"] == "NativeMonthConflictError"
    assert "gate" in report["restored"]
    assert report["restored"]["gate"] == []
