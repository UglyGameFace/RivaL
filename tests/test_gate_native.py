"""Realistic Gate historical contract-size, API repair and training guardrails."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import gate_archive_backfill as backfill
from parlay_bot.research import gate_native
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_bulk_archive import inspect, url_for
from parlay_bot.research.learning import _rows
from parlay_bot.research.source_native import native_manifest_provenance

MONTH = "2023-04"
START = 1680307200
MISSING = START + 23 * 300
COUNT = 8640
CLOCK = datetime(2026, 11, 1, tzinfo=UTC)


def archive_data(*, missing: int | None = None) -> bytes:
    return gzip.compress("".join(
        f"{START + i * 300},12,100,101,99,100\n"
        for i in range(COUNT)
        if START + i * 300 != missing
    ).encode())


def mock_market_api(*, missing_bar: bool = True,
                    config_change: int = 1600000000):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/contracts/BTC_USDT"):
            return httpx.Response(200, json={
                "name": "BTC_USDT", "type": "direct",
                "quanto_multiplier": "0.0001",
                "create_time": 1500000000,
                "config_change_time": config_change,
            })
        if request.url.path.endswith("/candlesticks"):
            assert request.url.params["contract"] == "BTC_USDT"
            assert request.url.params["interval"] == "5m"
            assert "limit" not in request.url.params
            assert request.url.params["timezone"] == "utc0"
            assert int(request.url.params["from"]) <= MISSING
            return httpx.Response(200, json=([{
                "t": MISSING, "v": "12", "c": "100", "h": "101",
                "l": "99", "o": "100",
            }] if missing_bar else []))
        return httpx.Response(404)
    return httpx.MockTransport(handler), requests


class Drive:
    def __init__(self):
        self.folders = SimpleNamespace(history="history", manifests="manifests")
        self.raw = []
        self.manifests = []
        self.verified = []
    async def ensure_history_file(self, *, artifact, batch_id, mime_type):
        assert mime_type == "application/x-ndjson"
        self.raw.append(artifact)
        return SimpleNamespace(file_id="native-file-id")
    async def ensure_manifest_file(self, *, artifact, batch_id):
        self.manifests.append(artifact)
        return SimpleNamespace(file_id="native-manifest-id")
    async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
        self.verified.append((file_id, parent_id, artifact.sha256))


async def qualified(tmp_path, *, missing: int | None = None,
                    missing_bar: bool = True, config_change: int = 1600000000):
    data = archive_data(missing=missing)
    path = tmp_path / "native-month-original.csv.gz"
    path.write_bytes(data)
    source = inspect(data, asset="BTC", kind="candlesticks_5m", month=MONTH)
    source["source_url"] = url_for(
        asset="BTC", kind="candlesticks_5m", month=MONTH,
    )
    transport, requests = mock_market_api(
        missing_bar=missing_bar, config_change=config_change,
    )
    drive = Drive()
    with httpx.Client(transport=transport) as client:
        receipt = await gate_native.qualify_and_publish(
            drive, client=client, archive=path, source=source,
            asset="BTC", month=MONTH, root=tmp_path, now=CLOCK,
        )
    return receipt, drive, requests


@pytest.mark.asyncio
async def test_complete_original_month_qualifies_only_with_dated_multiplier(tmp_path):
    result, drive, requests = await qualified(tmp_path)
    assert result["total_candles"] == COUNT
    assert result["same_venue_api_recovered"] == 0
    assert result["live_model_promoted"] is False
    assert len(drive.verified) == 2
    assert len(requests) == 1  # Contract metadata, no unnecessary candle API.
    meta, rows = _rows(drive.manifests[0].path)
    assert native_manifest_provenance(meta)
    assert meta["source_archive_rows"] == COUNT
    assert meta["source_api_recovered_count"] == 0
    assert meta["contract_multiplier_evidence"]["multiplier"] == 0.0001
    assert rows[0]["volume_base"] == pytest.approx(0.0012)
    assert rows[0]["volume_contracts"] == 12
    assert rows[-1]["opened_at"] == "2023-04-30T23:55:00+00:00"
    assert rows[0]["first_observed_at"] == CLOCK.isoformat()


@pytest.mark.asyncio
async def test_only_real_same_gate_api_recovers_missing_source_slot(tmp_path):
    result, drive, requests = await qualified(tmp_path, missing=MISSING)
    assert result["same_venue_api_recovered"] == 1
    meta, records = _rows(drive.manifests[0].path)
    assert meta["source_archive_rows"] == COUNT - 1
    assert meta["source_api_recovered_count"] == 1
    assert len(meta["source_api_recovery"]) == 1
    assert meta["source_api_recovery"][0]["recovered"] == [MISSING]
    assert len(meta["source_api_recovery"][0]["response_sha256"]) == 64
    assert len(records) == COUNT
    assert len(requests) == 2
    # Keep actual raw bytes, do not mutate the underlying official download.
    original = tmp_path / "native-month-original.csv.gz"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == result["source_raw_sha256"]


@pytest.mark.asyncio
async def test_unrecoverable_gate_source_gap_fails_without_any_native_model_input(tmp_path):
    with pytest.raises(HistoryImportError, match="did not recover"):
        await qualified(tmp_path, missing=MISSING, missing_bar=False)
    assert not list(tmp_path.glob("verified_venue_perp_klines/*.ndjson"))


@pytest.mark.asyncio
async def test_unproven_historical_multiplier_fails_closed(tmp_path):
    with pytest.raises(HistoryImportError, match="changed after"):
        await qualified(tmp_path, config_change=1680307201)
    assert not list(tmp_path.glob("verified_venue_perp_klines/*.ndjson"))


@pytest.mark.asyncio
async def test_api_response_only_allowed_from_exact_gate_contract_and_utc_window(tmp_path):
    data = archive_data(missing=MISSING)
    assert gate_native.gaps(
        gate_native.original_candles(
            (lambda p: (p.write_bytes(data), p)[1])(tmp_path / "raw.gz"),
            asset="BTC", month=MONTH,
            expected_sha=hashlib.sha256(data).hexdigest(),
        )[0],
        month=MONTH,
    ) == [MISSING]


def test_native_gate_provenance_rejects_claimed_provider_signature_and_forged_api(tmp_path):
    import asyncio
    receipt, drive, _ = asyncio.run(qualified(tmp_path, missing=MISSING))
    meta = json.loads(drive.manifests[0].path.read_text())
    assert native_manifest_provenance(meta)
    forged = {**meta, "provider_signed_checksum": True}
    assert native_manifest_provenance(forged) is False
    forged = {**meta, "source_archive_sha256": "0" * 64}
    assert native_manifest_provenance(forged) is False
    forged = {**meta, "source_archive_rows": meta["records"]}
    assert native_manifest_provenance(forged) is False
    assert receipt["source_training_eligible"] is True


@pytest.mark.asyncio
async def test_native_manifest_resume_inventory_validates_source_scope():
    names = [
        "gate-BTC_USDT-USDT-5m-2023-04--" + "a" * 16 + ".manifest.json",
        "unrelated.json",
    ]
    class Client:
        async def get(self, url, *, params, headers):
            return httpx.Response(200, json={"files": [{
                "name": name, "parents": ["manifests"],
                "appProperties": {"rival_kind": "manifest"},
            } for name in names]})
    class DriveMetadata:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="manifests")
        _client = Client()
        async def _verify_folder_boundary(self):
            pass
        async def _headers(self):
            return {}
        @staticmethod
        def _escape_query(value):
            return value
    assert await backfill.listed_native_months(DriveMetadata()) == {
        ("BTC", "2023-04"),
    }


def test_backfill_prioritizes_last_six_months_and_bounded_recertification():
    jobs = [
        ("BTC", "candlesticks_5m", m)
        for m in ["2023-01", "2023-03", "2026-05", "2026-06", "2026-07"]
    ] + [("BTC", "mark_prices", "2023-01")]
    selected = backfill.prioritize_backfill(
        jobs, existing={jobs[1]: "a" * 16},
        native=set(), months=["2023-01", "2023-03", "2026-05",
                               "2026-06", "2026-07"],
        now=CLOCK, max_attempts=4,
    )
    assert selected[0] == jobs[1]
    assert set(selected[1:]) == set(jobs[2:5])
    assert jobs[0] not in selected
