"""Bitget V3 source-exact archive: date windows, provenance and Drive readback."""
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research.bitget_v3_archive import (
    Pacer,
    bounds,
    collect_month,
    months,
    publish_month,
    validate_response,
    windows,
)
from parlay_bot.research.free_history import HistoryImportError


class NoWait(Pacer):
    async def wait(self):
        return None


def row(ts: int, *, premium: bool = False) -> list[str]:
    prices = (["-0.02", "0.01", "-0.03", "0.00"]
              if premium else ["100", "105", "95", "102"])
    return [str(ts), *prices, "0", "0"]


def mock_client(*, remove_one: bool = False, premium: bool = False,
                recorder: list | None = None) -> httpx.AsyncClient:
    first, _ = bounds("2021-06")

    def reply(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.bitget.com"
        assert req.url.path == "/api/v3/market/history-candles"
        assert req.url.params["category"] == "USDT-FUTURES"
        assert req.url.params["symbol"] == "BTCUSDT"
        assert req.url.params["interval"] == "5m"
        assert req.url.params["type"] == ("premium" if premium else "market")
        assert req.url.params["limit"] == "100"
        begin = int(req.url.params["startTime"]) + 1
        end = int(req.url.params["endTime"]) + 1
        assert end > begin and (end - begin) // 300000 <= 96
        if recorder is not None:
            recorder.append((begin, end))
        data = [
            row(stamp, premium=premium)
            for stamp in range(begin, end, 300000)
            if not (remove_one and stamp == first)
        ]
        return httpx.Response(200, json={"code": "00000", "data": data})

    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


def test_months_and_windows_are_complete_without_current_partial_month():
    clock = datetime(2026, 11, 1, tzinfo=UTC)
    calendar = months(clock, "2021-01")
    assert calendar[0] == "2021-01"
    assert calendar[-1] == "2026-10"
    assert len(calendar) == 70
    periods = windows("2021-06")
    first, end = bounds("2021-06")
    assert periods[0][0] == first and periods[-1][1] == end
    assert sum((b - a) // 300000 for a, b in periods) == 30 * 24 * 12
    assert all((b - a) // 300000 <= 96 for a, b in periods)
    with pytest.raises(HistoryImportError):
        bounds("2020-12")


@pytest.mark.parametrize("premium", [False, True])
@pytest.mark.asyncio
async def test_exact_complete_month_includes_original_verbatim_api_bytes(
    tmp_path: Path, premium: bool,
):
    calls = []
    client = mock_client(premium=premium, recorder=calls)
    try:
        raw, manifest, info = await collect_month(
            client, asset="BTC", family="premium" if premium else "market",
            month="2021-06", root=tmp_path, pacer=NoWait(),
            metadata={"metadata_sha256": "a" * 64},
        )
    finally:
        await client.aclose()
    assert len(calls) == 90
    assert info["candles"] == 8640
    assert info["request_pages"] == 90
    assert info["exact_utc_5m_grid_verified"]
    assert info["model_training_eligible"] is False
    assert info["provider_signed_checksum"] is False
    assert info["venue"] == "bitget"
    with gzip.open(raw, "rt") as source:
        first_line = json.loads(source.readline())
    assert first_line["request"]["type"] == ("premium" if premium else "market")
    assert first_line["response_sha256"]
    assert first_line["response_base64"]
    assert manifest.name.endswith(".manifest.json")


@pytest.mark.asyncio
async def test_missing_source_candle_blocks_month_not_synthesized(tmp_path: Path):
    client = mock_client(remove_one=True)
    try:
        with pytest.raises(HistoryImportError, match="incomplete UTC grid"):
            await collect_month(
                client, asset="BTC", family="market", month="2021-06",
                root=tmp_path, pacer=NoWait(),
                metadata={"metadata_sha256": "b" * 64},
            )
    finally:
        await client.aclose()
    assert not list(tmp_path.glob("*.manifest.json"))


def test_bad_prices_duplicates_and_mismatched_source_rejected():
    first, _ = bounds("2021-06")
    base = row(first)
    assert validate_response(
        json.dumps({"code": "00000", "data": [base]}).encode(),
        start=first, end=first + 300000, family="market",
    )[first] == base
    malicious = list(base)
    malicious[2] = "1"
    with pytest.raises(HistoryImportError, match="envelope"):
        validate_response(
            json.dumps({"code": "00000", "data": [malicious]}).encode(),
            start=first, end=first + 300000, family="market",
        )
    with pytest.raises(HistoryImportError, match="unreasonable Bitget price")
        validate_response(
            json.dumps({"code": "00000", "data": [
                [str(first), "NaN", "1", "1", "1", "0", "0"],
            ]}).encode(),
            start=first, end=first + 300000, family="market",
        )


@pytest.mark.asyncio
async def test_archive_and_manifest_both_require_drive_byte_readback(tmp_path: Path):
    raw = tmp_path / "a.gz"
    manifest = tmp_path / "a.manifest.json"
    raw.write_bytes(b"original-provider-response")
    manifest.write_text('{"venue":"bitget","model_training_eligible":false}')
    calls = []

    class FakeDrive:
        folders = SimpleNamespace(history="history", manifests="manifests")

        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            assert mime_type == "application/gzip"
            calls.append("save_history")
            return SimpleNamespace(file_id="raw-id")

        async def ensure_manifest_file(self, *, artifact, batch_id):
            calls.append("save_manifest")
            return SimpleNamespace(file_id="manifest-id")

        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            assert artifact.sha256 and artifact.size_bytes > 0
            calls.append("readback_" + parent_id)

    proof = await publish_month(FakeDrive(), raw=raw, manifest=manifest, count=2)
    assert proof["source_api_original_bytes_readback_verified"] is True
    assert calls == [
        "save_history", "readback_history",
        "save_manifest", "readback_manifests",
    ]


@pytest.mark.asyncio
async def test_http_451_cannot_be_retried_through_other_network_routes():
    from parlay_bot.research.bitget_v3_archive import request_page

    calls = []
    def blocked(req):
        calls.append(req)
        return httpx.Response(451)
    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    start, _ = bounds("2021-06")
    try:
        with pytest.raises(HistoryImportError, match="restricted HTTP 451"):
            await request_page(
                client, asset="BTC", family="market", begin=start,
                end=start + 96 * 300000, pacer=NoWait(),
            )
    finally:
        await client.aclose()
    assert len(calls) == 1
