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
        # Real Bitget source excludes the final 5m interval unless its
        # endTime is advanced by a complete interval.
        end = int(req.url.params["endTime"]) + 1 - 300000
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
    assert first_line["request"]["endTime"] == calls[0][1] + 300000 - 1
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
    with pytest.raises(HistoryImportError, match="unreasonable Bitget price"):
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


@pytest.mark.asyncio
async def test_incomplete_first_page_stops_before_wasting_rest_of_month(tmp_path: Path):
    requested = []
    client = mock_client(remove_one=True, recorder=requested)
    try:
        with pytest.raises(HistoryImportError, match="incomplete UTC grid"):
            await collect_month(
                client, asset="BTC", family="market", month="2021-06",
                root=tmp_path, pacer=NoWait(),
                metadata={"metadata_sha256": "a" * 64},
            )
    finally:
        await client.aclose()
    assert len(requested) == 2  # One exact-slot source retry, never fabricated.
    assert not list(tmp_path.glob("*.manifest.json"))


@pytest.mark.asyncio
async def test_public_probe_detects_missing_windows_without_drive_writes(tmp_path: Path):
    from parlay_bot.research.bitget_v3_archive import probe_source_windows

    observed = []
    def reply(req: httpx.Request):
        assert req.url.host == "api.bitget.com"
        assert req.url.path == "/api/v3/market/history-candles"
        begin = int(req.url.params["startTime"]) + 1
        end = int(req.url.params["endTime"]) + 1 - 300000
        month = datetime.fromtimestamp(begin / 1000, UTC).strftime("%Y-%m")
        family = req.url.params["type"]
        observed.append((month, family))
        keep = [
            row(tick, premium=family == "premium")
            for tick in range(begin, end, 300000)
            if not (month == "2025-06" and family == "mark" and tick == begin)
        ]
        return httpx.Response(200, json={"code": "00000", "data": keep})

    client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    try:
        result = await probe_source_windows(
            client=client, asset="BTC", report_dir=tmp_path,
            now=datetime(2026, 11, 1, tzinfo=UTC), pacer=NoWait(),
        )
    finally:
        await client.aclose()
    assert result["request_window_count"] == 27
    assert len(observed) == 27
    assert any(s["status"] == "incomplete_window" for s in result["samples"])
    assert result["complete_month_claimed"] is False
    assert result["drive_bytes_verified"] is False
    assert {s["variant"] for s in result["samples"] if "variant" in s} == {
        "start_back_one_bar", "end_forward_one_bar", "legacy_unpadded_end",
    }
    assert (tmp_path / "bitget-source-probe-BTC.json").exists()


@pytest.mark.asyncio
async def test_live_run_writes_incremental_checkpoint_before_source_failure(
    tmp_path: Path, monkeypatch,
):
    from parlay_bot.research import bitget_v3_archive as archive

    async def existing(drive, *, asset):
        return set()

    async def symbol(client, *, asset, pacer):
        return {"metadata_sha256": "a" * 64}

    async def fail_month(client, *, asset, family, month, root, pacer, metadata):
        report = json.loads(
            (tmp_path / "bitget-BTC-overnight-progress.json").read_text()
        )
        assert report["phase"] == "collecting"
        assert report["current_source_month"] == {"month": month, "family": family}
        raise HistoryImportError("incomplete UTC grid")

    monkeypatch.setattr(archive, "existing_manifests", existing)
    monkeypatch.setattr(archive, "verify_symbol", symbol)
    monkeypatch.setattr(archive, "collect_month", fail_month)
    drive = SimpleNamespace(folders=SimpleNamespace(reports=None))
    result = await archive.run(
        drive=drive, client=None, root=tmp_path / "raw",
        report_dir=tmp_path, asset="BTC", first="2021-01",
        max_requests=400, max_months=4,
        now=datetime(2021, 2, 1, tzinfo=UTC), pacer=NoWait(),
    )
    saved = json.loads(
        (tmp_path / "bitget-BTC-overnight-progress.json").read_text()
    )
    assert result["phase"] == "finished"
    assert saved["phase"] == "finished"
    assert saved["current_source_month"] is None
    assert len(saved["unavailable"]) == 4
    assert saved["source_months_published_and_readback"] == []


@pytest.mark.asyncio
async def test_live_proven_exclusive_end_needs_one_5m_padding_interval():
    from parlay_bot.research.bitget_v3_archive import request_page

    first, _ = bounds("2025-06")
    calls = []
    def boundary_api(req: httpx.Request):
        begin = int(req.url.params["startTime"]) + 1
        # Official API's observed end boundary excludes one final candle.
        returned_end = int(req.url.params["endTime"]) + 1 - 300000
        calls.append((begin, returned_end))
        return httpx.Response(200, json={
            "code": "00000",
            "data": [row(t) for t in range(begin, returned_end, 300000)],
        })
    client = httpx.AsyncClient(transport=httpx.MockTransport(boundary_api))
    try:
        _, missing = await request_page(
            client, asset="BTC", family="market", begin=first,
            end=first + 96 * 300000, pacer=NoWait(), end_forward_steps=0,
        )
        _, corrected = await request_page(
            client, asset="BTC", family="market", begin=first,
            end=first + 96 * 300000, pacer=NoWait(),
        )
    finally:
        await client.aclose()
    assert len(missing) == 95
    assert len(corrected) == 96
    assert first in corrected
    assert first + 95 * 300000 in corrected

@pytest.mark.asyncio
async def test_request_budget_counts_real_attempts_and_stops_before_exceeding_limit():
    from parlay_bot.research.bitget_v3_archive import RequestBudgetPacer

    pacer = RequestBudgetPacer(NoWait(), max_requests=2)
    await pacer.wait()
    await pacer.wait()
    assert pacer.attempted == 2
    with pytest.raises(HistoryImportError, match="request budget exhausted"):
        await pacer.wait()
    assert pacer.attempted == 2


@pytest.mark.asyncio
async def test_early_unavailable_months_do_not_consume_unrequested_page_budget(
    tmp_path: Path, monkeypatch,
):
    from parlay_bot.research import bitget_v3_archive as archive

    async def no_manifests(drive, *, asset):
        return set()

    async def verify(client, *, asset, pacer):
        await pacer.wait()  # Count the real contract-metadata API call.
        return {"metadata_sha256": "a" * 64}

    async def unavailable(client, *, asset, family, month, root, pacer, metadata):
        await pacer.wait()  # The first page was requested, 90 more were not.
        raise HistoryImportError("incomplete UTC grid")

    monkeypatch.setattr(archive, "existing_manifests", no_manifests)
    monkeypatch.setattr(archive, "verify_symbol", verify)
    monkeypatch.setattr(archive, "collect_month", unavailable)
    result = await archive.run(
        drive=SimpleNamespace(folders=SimpleNamespace(reports=None)),
        client=None, root=tmp_path / "raw", report_dir=tmp_path,
        asset="BTC", first="2021-01", max_requests=400, max_months=4,
        now=datetime(2021, 2, 1, tzinfo=UTC), pacer=NoWait(),
    )
    assert result["api_requests_attempted"] == 5
    assert result["api_request_pages_reserved"] > 5
    assert len(result["unavailable"]) == 4
    assert result["source_months_published_and_readback"] == []


def test_unattended_archive_frequency_budget_and_production_dependency_contract():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
                / "bitget-v3-overnight-archive.yml").read_text()
    assert 'cron: "17 0,4,8,12,16,20 * * *"' in workflow
    job = workflow.split("  bitget-overnight-to-drive:", 1)[1]
    assert "MAX_REQUESTS: ${{ inputs.max_requests || '8000' }}" in job
    assert "--first \"${{ inputs.first_month || '2021-01' }}\"" in job
    assert 'python -m pip install -e ".[archive]"' in job
    assert "pytest -q" not in job and "ruff check" not in job
    assert "cancel-in-progress: false" in job


@pytest.mark.asyncio
async def test_one_missing_source_bar_is_repaired_only_from_real_original_response(
    tmp_path: Path,
):
    first, _ = bounds("2021-06")
    calls = []

    def server(req):
        assert req.url.path == "/api/v3/market/history-candles"
        begin = int(req.url.params["startTime"]) + 1
        end = int(req.url.params["endTime"]) + 1 - 300000
        calls.append((begin, end))
        values = [
            row(tick) for tick in range(begin, end, 300000)
            if not (end - begin > 300000 and tick == first)
        ]
        return httpx.Response(200, json={"code": "00000", "data": values})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        raw, manifest, result = await collect_month(
            client, asset="BTC", family="market", month="2021-06",
            root=tmp_path, pacer=NoWait(), metadata={"metadata_sha256": "a" * 64},
        )
    assert result["exact_utc_5m_grid_verified"]
    assert result["targeted_repair_requests"] == 1
    assert result["request_pages"] == 91
    assert len(calls) == 91
    with gzip.open(raw, "rt") as restored:
        requests = [json.loads(line) for line in restored]
    assert len(requests) == 91
    assert any(record["request"]["startTime"] == first - 1 and
               record["request"]["endTime"] == first + 2 * 300000 - 1
               for record in requests)
    assert json.loads(manifest.read_text())["candles"] == 8640


@pytest.mark.asyncio
async def test_unattended_bitget_reads_only_same_asset_sha_verified_prior_status():
    import hashlib
    from parlay_bot.research.bitget_v3_archive import prior_source_decisions

    prior = {"asset": "BTC", "family": "premium", "month": "2021-03",
             "source_status": "source_returned_no_rows_not_permanently_proven",
             "last_checked_utc": "2026-11-02T14:00:00+00:00",
             "retry_after_utc": "2026-11-09T14:00:00+00:00"}
    body = json.dumps({
        "asset": "BTC", "schema_version": "rival-bitget-v3-overnight-progress-v1",
        "source_decisions": [prior],
    }).encode()

    def server(req):
        if req.url.path.endswith("/files"):
            return httpx.Response(200, json={"files": [{
                "id": "verifiedstatus1",
                "name": "bitget-BTC-overnight-progress.json",
                "size": len(body), "parents": ["reports"],
                "appProperties": {"rival_kind": "report",
                                  "rival_sha256": hashlib.sha256(body).hexdigest()},
            }]})
        assert req.url.path.endswith("/files/verifiedstatus1")
        return httpx.Response(200, content=body)

    class Fake:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(reports="reports")
        _escape_query = staticmethod(lambda x: x)

        async def _verify_folder_boundary(self):
            return None

        async def _headers(self):
            return {}

    drive = Fake()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        drive._client = client
        assert await prior_source_decisions(drive, asset="BTC") == {
            ("premium", "2021-03"): prior,
        }
        with pytest.raises(HistoryImportError, match="outside private boundary"):
            await prior_source_decisions(drive, asset="ETH")
