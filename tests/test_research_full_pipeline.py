from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.archive.drive import (
    DriveArchiveError,
    DriveArchiveFolders,
    GoogleDriveArchiveClient,
)
from parlay_bot.archive.models import ArchiveArtifact
from parlay_bot.research.audit import audit_catalog
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.learning import evaluate
from parlay_bot.research.production import run_pipeline


def sample_month(root, year, month, *, skip=frozenset()):
    start = datetime(year, month, 1, tzinfo=UTC)
    next_start = datetime(
        year + (month == 12), month % 12 + 1, 1, tzinfo=UTC,
    )
    total = int((next_start - start) / timedelta(hours=1))
    records = []
    for i in range(total):
        if i in skip:
            continue
        open_at = start + timedelta(hours=i)
        # Deterministic nontrivial price process with fixed known features
        # and only later target returns as outcomes.
        close = 100 + 0.02 * i + 3 * math.sin(i / 9)
        last = 100 if i == 0 else 100 + 0.02 * (i - 1) + 3 * math.sin((i - 1) / 9)
        records.append({
            "opened_at": open_at.isoformat(),
            "closed_at": (open_at + timedelta(hours=1)
                          - timedelta(milliseconds=1)).isoformat(),
            "open": last, "high": max(last, close) + 1,
            "low": min(last, close) - 1, "close": close,
            "volume_base": float(50 + i % 17), "trades": 40,
        })
    result = _persist(
        source="binance_um_klines", key=f"BTCUSDT-1h-{year}-{month:02d}",
        records=records, raw_sha="a" * 64,
        url=f"https://data.binance.vision/test/{year}-{month:02d}.zip",
        out_dir=root, extra={
            "venue": "binance", "contract": "BTCUSDT", "quote": "USDT",
            "interval_minutes": 60, "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
        },
    )
    return result.manifest_path


def test_exact_full_month_continuity_and_nonoverlap_model(tmp_path):
    first = sample_month(tmp_path, 2024, 1)
    second = sample_month(tmp_path, 2024, 2)
    a = audit_catalog(tmp_path, months=["2024-01", "2024-02"])
    assert a["all_examined_streams_qualified"] is True
    assert len(a["partitions"]) == 2
    assert a["streams"][0]["verified_rows"] == 1440
    result = evaluate([first, second], horizon_minutes=60)
    assert result["training_count"] >= 300
    assert result["out_of_sample_test"]["count"] >= 100
    assert result["live_model_promoted"] is False
    assert result["real_betting_profit_verified"] is False
    assert result["independent_live_forecast_accuracy"] is None
    assert result["model_spec"]["train_cutoff"] < result["model_spec"]["validation_cutoff"]
    repeat = evaluate([second, first], horizon_minutes=60)
    assert repeat["model_sha256"] == result["model_sha256"]


def test_bad_partition_or_incomplete_month_withholds_quality(tmp_path):
    sample_month(tmp_path, 2024, 1, skip=frozenset(range(40, 60)))
    audit = audit_catalog(tmp_path, months=["2024-01", "2024-02"])
    assert audit["all_examined_streams_qualified"] is False
    assert audit["streams"][0]["missing_months"] == ["2024-02"]
    assert audit["partitions"][0]["missing"] == 20
    assert audit["partitions"][0]["research_grade"] is False


def test_offline_rejects_hash_tampering_and_no_false_model_skill(tmp_path):
    first = sample_month(tmp_path, 2024, 1)
    second = sample_month(tmp_path, 2024, 2)
    first.parent.joinpath(json.loads(first.read_text())["file"]).write_bytes(b"corrupt")
    with pytest.raises(HistoryImportError):
        evaluate([first, second], horizon_minutes=60)


@pytest.mark.asyncio
async def test_missing_requested_source_blocks_pipeline(tmp_path):
    sample_month(tmp_path, 2024, 1)
    report = await run_pipeline(
        root=tmp_path, months=["2024-01", "2024-02"], assets=["BTC"],
        intervals=["1h"], seasons=[], leagues=[],
        collect_enabled=False, publish_enabled=False,
        max_new=1, report_dir=tmp_path / "reports",
    )
    assert report["all_requested_data_complete"] is False
    assert report["live_accuracy_improved_verified"] is False
    assert report["warehouse_status"] == "not_published_no_authorization"
    assert report["quality"]["missing_or_unqualified_streams"]


@pytest.mark.asyncio
async def test_complete_local_pipeline_offline_baseline_only(tmp_path):
    sample_month(tmp_path, 2024, 1)
    sample_month(tmp_path, 2024, 2)
    report = await run_pipeline(
        root=tmp_path, months=["2024-01", "2024-02"], assets=["BTC"],
        intervals=["1h"], seasons=[], leagues=[],
        collect_enabled=False, publish_enabled=False,
        max_new=1, report_dir=tmp_path / "reports",
    )
    assert report["all_requested_data_complete"] is True
    assert report["research"]["evaluations"]
    assert report["auto_betting_enabled"] is False


@pytest.mark.asyncio
async def test_actual_drive_restore_byte_hash_not_just_metadata(tmp_path):
    blob = b'{"research":true}\n'
    path = tmp_path / "report.json"
    path.write_bytes(blob)
    artifact = ArchiveArtifact(
        path=path, file_name=path.name, size_bytes=len(blob),
        sha256=hashlib.sha256(blob).hexdigest(),
        md5=hashlib.md5(blob, usedforsecurity=False).hexdigest(),
        row_count=1,
    )
    corrupted = False

    def handler(request: httpx.Request):
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"bad" if corrupted else blob)
        return httpx.Response(200, json={
            "id": "research123", "name": path.name,
            "size": str(len(blob)), "md5Checksum": artifact.md5,
            "parents": ["reports"], "trashed": False,
        })

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    drive = GoogleDriveArchiveClient(
        client_id="test", client_secret="secret", refresh_token="test",
        folders=DriveArchiveFolders(
            root="root", history="history", manifests="manifests",
            staging="staging", models="models", reports="reports",
        ),
        client=client,
    )
    drive._boundary_verified = True
    drive._token = "mocktoken"
    drive._token_expires_at = float("inf")
    try:
        await drive.verify_downloaded_artifact(
            file_id="research123", parent_id="reports", artifact=artifact,
        )
        corrupted = True
        with pytest.raises(DriveArchiveError):
            await drive.verify_downloaded_artifact(
                file_id="research123", parent_id="reports", artifact=artifact,
            )
    finally:
        await client.aclose()
