"""Rate-safe OKX research acceleration and archive pagination contracts."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import okx_bulk_bootstrap as bulk
from parlay_bot.research.source_native import restore_verified_source_months

BASE = "https://www.okx.com/api/v5/market/history-candles"


@pytest.mark.asyncio
async def test_shared_ip_pacer_across_four_clients():
    pacer = bulk.SharedOkxPacer(spacing=.16)
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"code": "0", "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as raw:
        clients = [bulk.PacedOkxClient(raw, pacer) for _ in range(4)]
        await asyncio.gather(*(client.get(BASE) for client in clients))
    assert len(requests) == 4
    assert pacer.requests == 4
    assert pacer.throttles == 0


@pytest.mark.asyncio
async def test_okx_api_50011_and_http_429_receive_bounded_retry(monkeypatch):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"code": "50011", "data": []})
        if count == 2:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"code": "0", "data": []})

    pacer = bulk.SharedOkxPacer()
    delays = []

    async def cooldown(delay):
        delays.append(delay)
        pacer.throttles += 1

    monkeypatch.setattr(pacer, "cooldown", cooldown)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as raw:
        result = await bulk.PacedOkxClient(raw, pacer).get(BASE)
    assert result.status_code == 200
    assert count == 3
    assert pacer.requests == 3
    assert delays == [4, 8]


@pytest.mark.asyncio
async def test_untrusted_400_is_not_retried():
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(400)

    pacer = bulk.SharedOkxPacer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as raw:
        response = await bulk.PacedOkxClient(raw, pacer).get(BASE)
    assert response.status_code == 400
    assert count == 1
    assert pacer.throttles == 0


@pytest.mark.asyncio
async def test_bulk_advances_all_assets_and_keeps_research_only(tmp_path, monkeypatch):
    attempts = defaultdict(int)
    limiters = set()
    closed = []
    class Drive:
        async def aclose(self):
            closed.append(True)

    async def fake_bootstrap(*, drive, client, asset, root, report_dir,
                             max_new_months, download_delay):
        attempts[asset] += 1
        limiters.add(id(client.pacer))
        assert download_delay == 0
        assert max_new_months == 6
        return {
            "asset": asset,
            "newly_published_months": ["2026-05"] if attempts[asset] == 1 else [],
            "full_2023_to_present_verified": attempts[asset] == 2,
        }

    monkeypatch.setattr(bulk, "_drive", lambda settings: Drive())
    monkeypatch.setattr(bulk, "bootstrap", fake_bootstrap)
    result = await bulk.run_bulk(
        assets=("BTC", "ETH", "SOL", "XRP"), passes=3,
        root=tmp_path / "raw", report_dir=tmp_path / "reports",
    )
    assert result["new_months_total"] == 4
    assert len(limiters) == 1
    assert len(closed) == 4
    assert result["complete"] is True
    assert result["live_model_promoted"] is False
    assert result["forward_model_improvement_verified"] is False


@pytest.mark.asyncio
async def test_bulk_one_blocked_source_does_not_erase_other_progress(tmp_path, monkeypatch):
    class Drive:
        async def aclose(self):
            return None

    async def fake_bootstrap(*, asset, **kwargs):
        if asset == "BTC":
            raise RuntimeError("source denied")
        return {"asset": asset, "newly_published_months": ["2026-05"],
                "full_2023_to_present_verified": False}

    monkeypatch.setattr(bulk, "_drive", lambda settings: Drive())
    monkeypatch.setattr(bulk, "bootstrap", fake_bootstrap)
    result = await bulk.run_bulk(
        assets=("BTC", "ETH"), passes=1,
        root=tmp_path / "raw", report_dir=tmp_path / "reports",
    )
    assert result["new_months_by_asset"] == {"BTC": 0, "ETH": 1}
    assert len(result["failures"]) == 1
    assert result["complete"] is False


@pytest.mark.asyncio
async def test_drive_native_manifest_listing_handles_multiple_pages(tmp_path):
    pages = []

    class Client:
        async def get(self, url, *, params, headers):
            assert url.endswith("/files")
            pages.append(params)
            files = [
                {"name": f"okx-BTC-USDT-SWAP-USDT-5m-2023-{i % 12 + 1:02d}--archive.manifest.json"}
                for i in range(100)
            ] if len(pages) == 1 else [
                {"name": "okx-ETH-USDT-SWAP-USDT-5m-2026-05--archive.manifest.json"}
            ]
            return httpx.Response(200, json={
                "files": files,
                **({"nextPageToken": "second-page"} if len(pages) == 1 else {}),
            })

    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="private-manifests")
        _client = Client()

        async def _verify_folder_boundary(self):
            return None

        async def _headers(self):
            return {}

        @staticmethod
        def _escape_query(s):
            return s

    rows = await restore_verified_source_months(
        Drive(), venue="okx", months=["2026-05"], root=tmp_path,
        contract="BTC-USDT-SWAP",
    )
    assert rows == []
    assert len(pages) == 2
    assert pages[1]["pageToken"] == "second-page"

@pytest.mark.asyncio
async def test_bulk_reports_independent_blocks_without_erasing_acquired_months(
    tmp_path, monkeypatch,
):
    class Drive:
        async def aclose(self):
            return None

    async def fake_bootstrap(*, asset, **kwargs):
        return {
            "asset": asset,
            "newly_published_months": ["2026-09"],
            "source_month_failures": [
                {"month": "2026-10", "reason": "official ZIP unavailable"}
            ],
            "native_research_error": "ambiguous candidate model duplicates",
            "advanced_catalog_failures": [
                {"month": "2026-09", "module": "trades",
                 "reason": "unverified host"}
            ],
            "full_2023_to_present_verified": False,
        }

    monkeypatch.setattr(bulk, "_drive", lambda settings: Drive())
    monkeypatch.setattr(bulk, "bootstrap", fake_bootstrap)
    result = await bulk.run_bulk(
        assets=("BTC",), passes=1,
        root=tmp_path / "raw", report_dir=tmp_path / "reports",
    )
    assert result["new_months_by_asset"] == {"BTC": 1}
    assert result["new_months_total"] == 1
    assert len(result["failures"]) == 2
    assert {row["stage"] for row in result["failures"]} == {
        "source_month", "offline_model_publication",
    }
    assert len(result["advanced_catalog_unverified"]) == 1
    assert result["complete"] is False
    import json
    saved = json.loads(
        (tmp_path / "reports" / "okx-bulk-progress.json").read_text()
    )
    assert saved["new_months_total"] == 1
    assert saved["live_model_promoted"] is False


def test_cli_recovered_throttles_do_not_invalidate_verified_history(
    monkeypatch, capsys,
):
    async def completed(**kwargs):
        return {
            "new_months_by_asset": {"BTC": 10},
            "requests": 2095, "throttles": 32, "failures": [],
            "complete": True, "advanced_catalog_unverified": [
                {"asset": "BTC", "month": "2026-01", "module": "trades"},
            ],
            "completion_scope": "okx_2023_to_present_5m_source_native_candles_only",
        }

    monkeypatch.setattr(bulk, "run_bulk", completed)
    assert bulk.main(["--asset", "BTC"]) == 0
    output = capsys.readouterr().out
    assert '"complete": true' in output
    assert '"advanced_catalog_unverified": 1' in output
    assert "::warning::" in output
    assert "32 transient cooldowns" in output


def test_cli_fails_on_actual_unverified_source_after_cooldowns(
    monkeypatch, capsys,
):
    async def blocked(**kwargs):
        return {
            "new_months_by_asset": {"BTC": 2},
            "requests": 99, "throttles": 3,
            "failures": [{"asset": "BTC", "month": "2025-10",
                          "stage": "source_month", "error": "archive failed"}],
            "complete": False, "advanced_catalog_unverified": [],
            "completion_scope": "okx_2023_to_present_5m_source_native_candles_only",
        }

    monkeypatch.setattr(bulk, "run_bulk", blocked)
    assert bulk.main(["--asset", "BTC"]) == 2
    output = capsys.readouterr().out
    assert "source_month" in output
    assert "3 transient cooldowns" in output


@pytest.mark.asyncio
async def test_okx_archive_stream_retries_429_before_using_candle_api(monkeypatch):
    responses = [429, 503, 200]
    seen = []

    def respond(request):
        status = responses[len(seen)]
        seen.append(status)
        return httpx.Response(status, content=b"source" if status == 200 else b"",
                              headers={"retry-after": "1"})

    pacer = bulk.SharedOkxPacer()
    delays = []

    async def cooldown(delay):
        delays.append(delay)
        pacer.throttles += 1

    monkeypatch.setattr(pacer, "cooldown", cooldown)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as raw:
        async with bulk.PacedOkxClient(raw, pacer).stream("GET", BASE) as response:
            assert response.status_code == 200
            assert await response.aread() == b"source"
    assert seen == [429, 503, 200]
    assert pacer.requests == 3
    assert delays == [4, 8]


@pytest.mark.asyncio
async def test_okx_archive_stream_exhaustion_remains_unverified(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(429)

    pacer = bulk.SharedOkxPacer()

    async def cooldown(delay):
        pacer.throttles += 1

    monkeypatch.setattr(pacer, "cooldown", cooldown)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as raw:
        async with bulk.PacedOkxClient(raw, pacer).stream("GET", BASE) as response:
            assert response.status_code == 429
    assert len(seen) == bulk.MAX_RETRIES
    assert pacer.requests == bulk.MAX_RETRIES


@pytest.mark.asyncio
async def test_okx_pacer_slows_shared_traffic_on_throttle():
    pacer = bulk.SharedOkxPacer()
    assert pacer.spacing == bulk.MIN_SPACING
    for _ in range(30):
        await pacer.cooldown(0)
    assert pacer.throttles == 30
    assert pacer.spacing == 0.5
