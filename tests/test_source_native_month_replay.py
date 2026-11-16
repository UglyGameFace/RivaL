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

    def __init__(
        self, *, changed_manifest=False, corrupt_replay=False,
        first_data=None, second_data=None, corrupt_second_history=False,
    ):
        self.folders = SimpleNamespace(manifests="manifests", history="history")
        self.data = (first_data if first_data is not None
                     else _bytes({"closed_at": "2026-09-01T00:05:00+00:00"}) + bytes((10,)))
        self.second_data = second_data if second_data is not None else self.data
        file = "okx-BTC-USDT-SWAP-USDT-5m-2026-09--0000000000000001.ndjson"
        second_file = (
            file if self.second_data == self.data
            else "okx-BTC-USDT-SWAP-USDT-5m-2026-09--"
                 + hashlib.sha256(self.second_data).hexdigest()[:16] + ".ndjson"
        )
        doc = {
            "file": file, "source": SOURCE, "venue": "okx",
            "contract": "BTC-USDT-SWAP", "quote": "USDT",
            "interval_minutes": 5, "source_integrity": "postgres-first-observed-at-v1",
            "prospective_forecast": False, "live_trading_authorized": False,
            "data_sha256": hashlib.sha256(self.data).hexdigest(),
            "source_sha256": "e" * 64,
        }
        second_doc = {
            **doc, "file": second_file,
            "data_sha256": hashlib.sha256(self.second_data).hexdigest(),
            "source_sha256": "f" * 64 if self.second_data != self.data else "e" * 64,
        }
        if changed_manifest:
            second_doc["different_provenance"] = "new"
        first = _bytes(doc)
        second = _bytes(second_doc)
        self.blobs = {
            "m1": first, "m2": first if corrupt_replay else second,
            "h1": self.data,
            "h2": b"corrupt" if corrupt_second_history else self.second_data,
        }
        self.manifests = [
            {"id": key, "name": filename.removesuffix(".ndjson") + ".manifest.json",
             "size": str(len(blob)), "parents": ["manifests"],
             "appProperties": {"rival_kind": "manifest",
                               "rival_sha256": hashlib.sha256(blob).hexdigest()}}
            for key, blob, filename in (
                ("m1", first, file), ("m2", second, second_file),
            )
        ]
        if corrupt_replay:
            # Metadata authenticates the original second manifest, but the
            # returned second object is tampered.
            self.manifests[1]["appProperties"]["rival_sha256"] = (
                hashlib.sha256(second).hexdigest()
            )
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
        first = "native-" + hashlib.sha256(self.data).hexdigest()[:24]
        second = "native-" + hashlib.sha256(self.second_data).hexdigest()[:24]
        if batch_id == first:
            key, size, i = "h1", len(self.data), 0
        elif batch_id == second:
            key, size, i = "h2", len(self.second_data), 1
        else:
            raise AssertionError("unexpected normalized source SHA")
        file = self.manifests[i]["name"].removesuffix(".manifest.json") + ".ndjson"
        return SimpleNamespace(file_id=key, name=file, size_bytes=size)

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
    assert evidence["schema"] == "rival-native-month-conflict-v2"
    assert evidence["resolution"] == "blocked_divergent_manifest_bytes"
    assert evidence["first"]["manifest_sha256"] != evidence["second"]["manifest_sha256"]
    assert evidence["first"]["claimed_history_sha256"] == hashlib.sha256(drive.data).hexdigest()
    assert evidence["second"]["claimed_history_sha256"] == hashlib.sha256(drive.data).hexdigest()
    assert evidence["first"]["claimed_provider_source_sha256"] == "e" * 64
    assert evidence["second"]["claimed_provider_source_sha256"] == "e" * 64
    assert evidence["first"]["source_integrity"] == "postgres-first-observed-at-v1"
    assert evidence["verification"] == {
        "both_manifests_sha_verified": True,
        "first_history_readback_verified": True,
        "second_history_readback_verified": True,
        "second_provider_original_witness_verified": False,
    }
    assert evidence["comparison"] == {
        "first_rows": 1,
        "second_rows": 1,
        "all_candle_fields_identical": True,
        "identical_except_first_observed_at": True,
    }
    assert "m1" not in json.dumps(evidence)
    assert "m2" not in json.dumps(evidence)
    assert "source_url" not in json.dumps(evidence)
    assert drive.calls.count("m2") == 1


def _observation_row(*, observed_at: str, close: float = 101) -> bytes:
    record = {
        "venue": "okx", "contract": "BTC-USDT-SWAP",
        "quote": "USDT", "interval_minutes": 5,
        "opened_at": "2026-09-01T00:00:00+00:00",
        "closed_at": "2026-09-01T00:04:59.999000+00:00",
        "first_observed_at": observed_at,
        "open": 100, "high": 102, "low": 99, "close": close,
        "volume_base": 1, "volume_quote": 100,
    }
    return _bytes(record) + bytes((10,))


@pytest.mark.asyncio
async def test_conflicting_source_sha_can_be_observation_only(tmp_path):
    first = _observation_row(observed_at="2026-10-01T00:00:00+00:00")
    second = _observation_row(observed_at="2026-11-01T00:00:00+00:00")
    drive = _Drive(changed_manifest=True, first_data=first, second_data=second)
    with pytest.raises(NativeMonthConflictError) as caught:
        await restore_verified_source_months(
            drive, venue="okx", contract="BTC-USDT-SWAP",
            months=["2026-09"], root=tmp_path,
        )
    proof = caught.value.evidence
    assert proof["comparison"] == {
        "first_rows": 1, "second_rows": 1,
        "all_candle_fields_identical": False,
        "identical_except_first_observed_at": True,
    }
    assert proof["verification"]["second_history_readback_verified"] is True
    assert proof["first"]["claimed_history_sha256"] != proof["second"]["claimed_history_sha256"]
    assert "2026-10" not in json.dumps(proof)
    assert "2026-11" not in json.dumps(proof)
    assert drive.calls.count("h2") == 1


@pytest.mark.asyncio
async def test_conflicting_sources_with_price_changes_remain_blocked(tmp_path):
    first = _observation_row(observed_at="2026-10-01T00:00:00+00:00", close=101)
    second = _observation_row(observed_at="2026-11-01T00:00:00+00:00", close=102)
    drive = _Drive(changed_manifest=True, first_data=first, second_data=second)
    with pytest.raises(NativeMonthConflictError) as caught:
        await restore_verified_source_months(
            drive, venue="okx", contract="BTC-USDT-SWAP",
            months=["2026-09"], root=tmp_path,
        )
    assert caught.value.evidence["comparison"]["identical_except_first_observed_at"] is False
    assert caught.value.evidence["resolution"] == "blocked_divergent_manifest_bytes"


@pytest.mark.asyncio
async def test_conflicting_source_second_original_tampering_fails_closed(tmp_path):
    first = _observation_row(observed_at="2026-10-01T00:00:00+00:00")
    second = _observation_row(observed_at="2026-11-01T00:00:00+00:00")
    drive = _Drive(
        changed_manifest=True, first_data=first, second_data=second,
        corrupt_second_history=True,
    )
    with pytest.raises(HistoryImportError, match="second history SHA mismatch"):
        await restore_verified_source_months(
            drive, venue="okx", contract="BTC-USDT-SWAP",
            months=["2026-09"], root=tmp_path,
        )
    assert drive.calls.count("h2") == 1


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
