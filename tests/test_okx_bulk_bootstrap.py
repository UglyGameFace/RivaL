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
