"""Bounded Gate API fallback and source provenance regression coverage."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import gate_api_fallback, gate_archive_backfill
from parlay_bot.research.free_history import HistoryImportError


class EvidenceDrive:
    folders = SimpleNamespace(history="verified-history")

    def __init__(self):
        self.uploads = []
        self.readbacks = []

    async def ensure_history_file(self, *, artifact, batch_id, mime_type):
        assert batch_id == "gateapi-" + artifact.sha256[:24]
        assert mime_type == "application/json"
        self.uploads.append(artifact)
        return SimpleNamespace(file_id=artifact.sha256)

    async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
        assert file_id == artifact.sha256
        assert parent_id == self.folders.history
        self.readbacks.append(artifact.sha256)


@pytest.mark.asyncio
async def test_full_same_gate_api_month_is_preserved_but_not_trained(tmp_path):
    month = "2023-02"
    start = int(datetime(2023, 2, 1, tzinfo=UTC).timestamp())
    calls = []

    def handler(request):
        assert request.url.path.endswith("/futures/usdt/candlesticks")
        assert request.url.params["contract"] == "BTC_USDT"
        assert request.url.params["timezone"] == "utc0"
        low = int(request.url.params["from"])
        end = int(request.url.params["to"])
        calls.append((low, end))
        return httpx.Response(200, json=[
            {"t": stamp, "v": "12", "o": "100", "h": "101",
             "l": "99", "c": "100"}
            for stamp in range(low, end + 1, 300)
        ])
    drive = EvidenceDrive()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = await gate_api_fallback.recover_unavailable_archive(
            drive, client=client, asset="BTC", month=month,
            root=tmp_path, now=datetime(2026, 11, 1, tzinfo=UTC),
        )
    assert len(calls) == 9
    assert result["candles"] == 8064
    assert result["api_windows"] == 9
    assert result["model_training_eligible"] is False
    assert len(drive.uploads) == 10
    assert len(drive.readbacks) == 10
    receipt = json.loads(drive.uploads[-1].path.read_text())
    assert receipt["calendar_5m_complete"] is True
    assert receipt["source_transport"] == "official_gate_v4_public_api"
    assert receipt["original_provider_archive_available"] is False
    assert all(item["raw_json_file"].startswith("gate-api-")
               for item in receipt["windows"])


@pytest.mark.asyncio
async def test_incomplete_api_month_fails_before_any_evidence_is_published(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])
    drive = EvidenceDrive()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HistoryImportError, match="coverage unavailable"):
            await gate_api_fallback.recover_unavailable_archive(
                drive, client=client, asset="BTC", month="2023-02",
                root=tmp_path, now=datetime(2026, 11, 1, tzinfo=UTC),
            )
    assert calls == 1
    assert not drive.uploads


@pytest.mark.asyncio
async def test_gate_404_fallback_is_bounded_and_never_counts_as_native(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_archive_backfill, "complete_months",
                        lambda clock: ["2023-02"])
    async def listed(drive):
        return {}
    async def native(drive):
        return set()
    monkeypatch.setattr(gate_archive_backfill, "listed_archives", listed)
    monkeypatch.setattr(gate_archive_backfill, "listed_native_months", native)
    monkeypatch.setattr(gate_archive_backfill, "rotate_attempts",
                        lambda jobs, *, now, count: jobs[:count])

    def missing_bulk(*args, **kwargs):
        raise ValueError("Gate public archive unavailable: HTTP 404")
    monkeypatch.setattr(gate_archive_backfill, "download", missing_bulk)
    calls = []
    async def fallback(*args, **kwargs):
        calls.append(kwargs["asset"])
        return {"asset": kwargs["asset"], "month": kwargs["month"],
                "status": "api_complete_research_only",
                "model_training_eligible": False}
    monkeypatch.setattr(gate_api_fallback, "recover_unavailable_archive", fallback)
    result = await gate_archive_backfill.collect_gate(
        drive=object(), root=tmp_path / "data", report_dir=tmp_path / "reports",
        assets=("BTC", "ETH"), kinds=("candlesticks_5m",),
        max_new=1, max_attempts=2,
        now=datetime(2023, 3, 1, tzinfo=UTC),
    )
    assert calls == ["BTC"]
    assert result["native_qualified"] == []
    assert result["published"] == []
    assert result["same_venue_api_fallback"][0]["model_training_eligible"] is False
    assert len(result["unavailable_or_rejected"]) == 2
    assert result["model_approved"] is False
