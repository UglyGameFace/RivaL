"""Original Gate one-minute candle proof, strict cross-source consistency."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import gate_minute_repair as minutes
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_bulk_archive import inspect, url_for

START = 1680307200
MONTH = "2023-04"
MISSING = START + 4 * 300
N_5M = 8640


def five_bars():
    return {
        START + i * 300: {
            "open": 100.0, "close": 100.0,
            "high": 101.0, "low": 99.0,
            "volume_contracts": 10.0,
        }
        for i in range(N_5M) if START + i * 300 != MISSING
    }


def minute_data(*, lost: int | None = None,
                duplicate: bool = False) -> bytes:
    source = []
    for i in range(20 * 5):
        t = START + i * 60
        if t != lost:
            source.append(f"{t},2,100,101,99,100\n")
    if duplicate:
        source.append(f"{START},2,100,101,99,100\n")
    return gzip.compress("".join(source).encode())


def write_minute(tmp_path: Path, payload: bytes) -> Path:
    p = tmp_path / "minutes.csv.gz"
    p.write_bytes(payload)
    return p


def test_exact_five_same_gate_minutes_repair_missing_source_bar(tmp_path):
    raw = minute_data()
    archive = write_minute(tmp_path, raw)
    stamps, info = minutes.minute_rows(
        archive, asset="SOL", month=MONTH,
        expected_sha=hashlib.sha256(raw).hexdigest(),
    )
    assert info["rows"] == 100
    reconstructed, report = minutes.reconstruct_gaps(
        original_5m=five_bars(), one_minute=stamps,
        asset="SOL", month=MONTH, original_sha="1" * 64,
        minute_sha=hashlib.sha256(raw).hexdigest(),
    )
    assert list(reconstructed) == [MISSING]
    assert reconstructed[MISSING]["volume_contracts"] == 10
    assert reconstructed[MISSING]["open"] == 100
    assert report["crosschecked_5m_source_bars"] >= 4
    assert report["missing_source_5m_before"] == 1
    assert report["unresolved_open_unix_seconds"] == []
    assert report["model_training_eligible"] is False
    assert report["not_interpolated"] is True
    assert report["source_1m_reconstructions"][0]["actual_1m_source_timestamps"] == [
        MISSING + i * 60 for i in range(5)
    ]


def test_missing_real_one_minute_never_generates_a_five_minute_bar(tmp_path):
    raw = minute_data(lost=MISSING + 120)
    stamps, _ = minutes.minute_rows(
        write_minute(tmp_path, raw), asset="SOL", month=MONTH,
        expected_sha=hashlib.sha256(raw).hexdigest(),
    )
    derived, proof = minutes.reconstruct_gaps(
        original_5m=five_bars(), one_minute=stamps,
        asset="SOL", month=MONTH, original_sha="1" * 64,
        minute_sha=hashlib.sha256(raw).hexdigest(),
    )
    assert derived == {}
    assert proof["unresolved_open_unix_seconds"] == [MISSING]
    assert proof["five_minute_candles_reconstructed_from_exact_5x_1m"] == 0


def test_incompatible_gate_minute_archive_never_used_for_repair(tmp_path):
    raw = minute_data()
    stamps, _ = minutes.minute_rows(
        write_minute(tmp_path, raw), asset="SOL", month=MONTH,
        expected_sha=hashlib.sha256(raw).hexdigest(),
    )
    stamps[START + 240]["close"] = 150  # Final minute determines 5m close
    with pytest.raises(HistoryImportError, match="disagrees"):
        minutes.reconstruct_gaps(
            original_5m=five_bars(), one_minute=stamps,
            asset="SOL", month=MONTH, original_sha="1" * 64,
            minute_sha=hashlib.sha256(raw).hexdigest(),
        )


def test_duplicate_gate_minute_timestamp_fail_closed(tmp_path):
    raw = minute_data(duplicate=True)
    with pytest.raises(HistoryImportError, match="duplicate"):
        minutes.minute_rows(
            write_minute(tmp_path, raw), asset="SOL", month=MONTH,
            expected_sha=hashlib.sha256(raw).hexdigest(),
        )


def test_modified_original_archive_sha_blocks_source_provenance(tmp_path):
    raw = minute_data()
    with pytest.raises(HistoryImportError, match="checksum"):
        minutes.minute_rows(
            write_minute(tmp_path, raw), asset="SOL", month=MONTH,
            expected_sha="0" * 64,
        )


@pytest.mark.asyncio
async def test_actual_original_1m_and_derived_evidence_immutable_drive_readback(
    tmp_path, monkeypatch,
):
    raw = minute_data()
    def fake_download(*, client, asset, kind, month, output):
        assert asset == "SOL"
        assert kind == "candlesticks_1m"
        assert month == MONTH
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        receipt = inspect(raw, asset=asset, kind=kind, month=month)
        receipt["source_url"] = url_for(asset=asset, kind=kind, month=month)
        return receipt
    monkeypatch.setattr(minutes, "download", fake_download)

    class FakeDrive:
        folders = SimpleNamespace(history="history", manifests="manifests",
                                  reports="reports")
        def __init__(self):
            self.history = []
            self.manifests = []
            self.reports = []
            self.verified = []

        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            assert mime_type == "application/gzip"
            self.history.append(artifact)
            return SimpleNamespace(file_id="original-1m")
        async def ensure_manifest_file(self, *, artifact, batch_id):
            self.manifests.append(artifact)
            return SimpleNamespace(file_id="original-1m-receipt")
        async def ensure_research_output(self, *, artifact, batch_id, kind):
            assert kind == "report"
            self.reports.append(artifact)
            return SimpleNamespace(file_id="derived-5m-report")
        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            self.verified.append((file_id, parent_id, artifact.sha256))
    drive = FakeDrive()
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(404)
    )) as client:
        result = await minutes.publish_minute_gap_evidence(
            drive, client=client, original_5m=five_bars(),
            original_5m_sha="1" * 64, asset="SOL", month=MONTH,
            root=tmp_path,
        )
    assert result["same_gate_1m_candles_derived"] == 1
    assert result["original_minute_drive_sha_readback_verified"] is True
    assert result["repair_receipt_drive_sha_readback_verified"] is True
    assert result["model_training_eligible"] is False
    assert len(drive.verified) == 3
    assert drive.history[0].path.read_bytes() == raw
    report = json.loads(drive.reports[0].path.read_text())
    assert report["source_original_1m_file"].endswith(".csv.gz")
    assert report["five_minute_candles_reconstructed_from_exact_5x_1m"] == 1
    assert report["historical_contract_multiplier_verified"] is False
