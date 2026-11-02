"""Regression gates for original Bitget Drive months entering offline ML."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.research.audit import audit_partition
from parlay_bot.research.bitget_source_bridge import restore_verified_bitget_market_months
from parlay_bot.research.bitget_v3_archive import (
    ENDPOINT, STEP, bounds, windows, write_original_responses,
)
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows
from parlay_bot.research.source_native import SOURCE, native_manifest_provenance
from parlay_bot.research.source_native_offline import run_native_research

MONTH = "2026-05"


def _original(tmp_path: Path, *, missing: bool = False):
    start, end = bounds(MONTH)
    entries = []
    combined = hashlib.sha256()
    for page_no, (begin, finish) in enumerate(windows(MONTH)):
        raw_rows = []
        for tick in range(begin, finish, STEP):
            if missing and page_no == 2 and tick == begin + 2 * STEP:
                continue
            raw_rows.append([
                str(tick), "99", "102", "98", "101", "10", "1010",
            ])
        # Real provider includes one earlier boundary candle in some windows.
        raw_rows.insert(0, [
            str(begin - STEP), "99", "102", "98", "101", "10", "1010",
        ])
        payload = json.dumps({"code": "00000", "data": raw_rows}).encode()
        combined.update(payload)
        entries.append({
            "request": {
                "endpoint": ENDPOINT, "category": "USDT-FUTURES",
                "symbol": "BTCUSDT", "type": "market", "interval": "5m",
                "startTime": begin - 1, "endTime": finish + STEP - 1,
                "limit": 100,
            },
            "response_sha256": hashlib.sha256(payload).hexdigest(),
            "response_base64": base64.b64encode(payload).decode(),
        })
    saved = tmp_path / "source.gz"
    raw_hash = write_original_responses(saved, entries)
    raw_name = f"bitget-v3-BTC-market-{MONTH}--{raw_hash[:16]}"
    manifest = {
        "schema_version": "rival-bitget-v3-raw-api-month-v1",
        "venue": "bitget", "asset": "BTC", "contract": "BTCUSDT",
        "category": "USDT-FUTURES", "market_type": "usdt_linear_perpetual",
        "quote": "USDT", "family": "market", "interval_minutes": 5,
        "utc_month": MONTH, "source_url": ENDPOINT,
        "source_transport": "bitget_public_v3_api",
        "raw_file": raw_name + ".responses.jsonl.gz", "raw_sha256": raw_hash,
        "concatenated_original_response_sha256": combined.hexdigest(),
        "source_metadata_sha256": "a" * 64,
        "historical_listing_date_verified": False,
        "provider_signed_checksum": False,
        "request_pages": len(windows(MONTH)),
        "candles": (end - start) // STEP,
        "exact_utc_5m_grid_verified": True,
        "first_observed_at": "2026-11-02T06:34:10+00:00",
        "retrospective": True, "prospective_forecast": False,
        "model_training_eligible": False, "live_trading_authorized": False,
    }
    original = saved.read_bytes()
    text = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    return raw_name, raw_hash, original, text


class _Drive:
    DRIVE_URL = "https://www.googleapis.com/drive/v3"
    folders = SimpleNamespace(manifests="manifests", history="history")
    _escape_query = staticmethod(lambda x: x)

    def __init__(self, name, digest, raw, manifest):
        self.name = name
        self.digest = digest
        self.raw = raw
        self.manifest = manifest
        self.calls = []
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._request),
        )

    async def _verify_folder_boundary(self):
        return None

    async def _headers(self):
        return {}

    @staticmethod
    def _object(data):
        return SimpleNamespace(
            name=data["name"], file_id=data["id"],
            size_bytes=int(data["size"]), parents=tuple(data["parents"]),
        )

    async def _find_batch_object(self, *, parent_id, batch_id, kind):
        assert (parent_id, batch_id, kind) == (
            "history", "bitgetv3-" + self.digest[:24], "history",
        )
        return SimpleNamespace(
            name=self.name + ".responses.jsonl.gz",
            file_id="raw", size_bytes=len(self.raw),
        )

    def _request(self, request):
        self.calls.append(str(request.url))
        if request.url.path.endswith("/files"):
            assert "bitget-v3-BTC-market-" in request.url.params["q"]
            return httpx.Response(200, json={"files": [{
                "id": "manifest",
                "name": self.name + ".manifest.json",
                "size": str(len(self.manifest)), "parents": ["manifests"],
                "appProperties": {
                    "rival_kind": "manifest",
                    "rival_sha256": hashlib.sha256(self.manifest).hexdigest(),
                },
            }]})
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, content=self.manifest)
        if request.url.path.endswith("/raw"):
            return httpx.Response(200, content=self.raw)
        raise AssertionError("unexpected Drive request")


@pytest.mark.asyncio
async def test_original_bitget_month_enters_existing_audit_and_model_format(tmp_path):
    name, digest, raw, manifest = _original(tmp_path)
    drive = _Drive(name, digest, raw, manifest)
    try:
        restored = await restore_verified_bitget_market_months(
            drive, months=[MONTH], root=tmp_path / "research", asset="BTC",
        )
    finally:
        await drive._client.aclose()
    assert len(restored) == 1
    info, rows = _rows(restored[0])
    assert info["source"] == SOURCE
    assert native_manifest_provenance(info)
    assert info["source_archive_sha256"] == digest
    assert info["trades_observed"] is False
    assert info["provider_signed_checksum"] is False
    assert info["source_original_drive_readback_checked"] is True
    assert info["prospective_forecast"] is False
    assert info["live_trading_authorized"] is False
    assert len(rows) == 8928
    assert rows[0]["opened_at"] == "2026-05-01T00:00:00+00:00"
    assert rows[-1]["opened_at"] == "2026-05-31T23:55:00+00:00"
    assert all(row["trades"] == -1 for row in rows)
    assert audit_partition(restored[0])["full_calendar_month_verified"] is True
    assert len(drive.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "raw-tamper", "metadata-tamper"])
async def test_original_bitget_bad_month_cannot_train(tmp_path, fault):
    name, digest, raw, manifest = _original(tmp_path, missing=fault == "missing")
    if fault == "raw-tamper":
        raw += b"garbage"
    if fault == "metadata-tamper":
        doc = json.loads(manifest)
        doc["family"] = "premium"
        manifest = json.dumps(doc).encode()
    drive = _Drive(name, digest, raw, manifest)
    try:
        with pytest.raises(HistoryImportError):
            await restore_verified_bitget_market_months(
                drive, months=[MONTH], root=tmp_path / "research", asset="BTC",
            )
    finally:
        await drive._client.aclose()
    assert not list((tmp_path / "research" / SOURCE).glob("*.manifest.json"))


@pytest.mark.asyncio
async def test_single_original_month_does_not_bypass_six_month_screen(tmp_path):
    name, digest, raw, manifest = _original(tmp_path)
    drive = _Drive(name, digest, raw, manifest)
    try:
        result = await run_native_research(
            drive=drive, venue="bitget",
            months=[f"2026-{m:02d}" for m in range(5, 11)],
            asset="BTC", root=tmp_path / "research",
            report_dir=tmp_path / "reports", publish=True,
        )
    finally:
        await drive._client.aclose()
    assert result["status"] == "source_months_incomplete"
    assert result["qualified_source_scopes"] == 0
    assert result["published_models"] == 0
    assert result["promoted_live"] is False


def test_fake_bitget_provenance_never_admitted():
    assert not native_manifest_provenance({
        "source_integrity": "bitget-v3-original-api-5m-v1",
        "venue": "bitget", "source_original_drive_readback_checked": True,
        "provider_signed_checksum": True,
    })
