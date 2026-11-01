"""Verified official OKX historical research, never speculative exchange data."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research import official_okx_bootstrap as okx
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows

START = datetime(2026, 5, 1, tzinfo=UTC)
END = START + timedelta(minutes=510)
OBSERVED = datetime(2026, 11, 1, tzinfo=UTC)


def _response(request, *, hole: int | None = None, untrusted: bool = False):
    if request.url.path.endswith("/instruments"):
        return httpx.Response(200, json={"code": "0", "data": [{
            "instId": "ETH-USDT-SWAP" if untrusted else "BTC-USDT-SWAP",
            "instType": "SWAP", "settleCcy": "USDT",
            "ctType": "linear",
        }]})
    if request.url.path.endswith("/history-candles"):
        assert request.url.params["bar"] == "5m"
        assert request.url.params["instId"] == "BTC-USDT-SWAP"
        boundary = int(request.url.params["after"])
        candles = [
            [str(int(START.timestamp() * 1000) + index * 300_000),
             "100", "105", "99", "101", "10", "0.5", "50", "1"]
            for index in range(102) if index != hole
            and int(START.timestamp() * 1000) + index * 300_000 < boundary
        ]
        candles.sort(key=lambda row: int(row[0]), reverse=True)
        return httpx.Response(200, json={"code": "0", "data": candles[:100]})
    if request.url.path.endswith("/market-data-history"):
        return httpx.Response(200, json={"code": "0", "data": [{
            "dateAggrType": "monthly", "details": [{
                "instFamily": "BTC-USDT", "instType": "SWAP",
                "groupDetails": [{
                    "filename": "BTC-USDT-SWAP-trades-2026-05.zip",
                    "url": ("https://static.okx.com/cdn/okex/traderecords/trades/"
                            "monthly/202605/BTC-USDT-SWAP-trades-2026-05.zip"),
                }],
            }],
        }]})
    raise AssertionError("unexpected OKX public request")


@pytest.mark.asyncio
async def test_okx_full_month_source_native_strict_pages_and_units(tmp_path, monkeypatch):
    monkeypatch.setattr(okx, "month_bounds", lambda month: (START, END))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_response),
    ) as client:
        assert await okx.verify_contract(client, "BTC") == "BTC-USDT-SWAP"
        result = await okx.download_month(
            client, asset="BTC", month="2026-05",
            root=tmp_path, now=OBSERVED, delay=0,
        )
    meta, rows = _rows(result.manifest_path)
    assert len(rows) == 102
    assert meta["source_integrity"] == okx.INTEGRITY
    assert meta["provider_signed_checksum"] is False
    assert meta["prospective_forecast"] is False
    assert rows[0]["opened_at"] == START.isoformat()
    assert rows[-1]["opened_at"] == (END - timedelta(minutes=5)).isoformat()
    assert rows[0]["volume_base"] == .5
    assert rows[0]["volume_contracts"] == 10
    assert rows[0]["volume_quote"] == 50
    assert all(row["first_observed_at"] == OBSERVED.isoformat() for row in rows)


@pytest.mark.asyncio
async def test_okx_missing_historical_slot_refuses_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(okx, "month_bounds", lambda month: (START, END))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: _response(req, hole=17)),
    ) as client:
        with pytest.raises(HistoryImportError):
            await okx.download_month(
                client, asset="BTC", month="2026-05",
                root=tmp_path, now=OBSERVED, delay=0,
            )
    assert not list(tmp_path.rglob("*.ndjson"))


@pytest.mark.asyncio
async def test_okx_invalid_instrument_never_becomes_native_data():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: _response(req, untrusted=True)),
    ) as client:
        with pytest.raises(HistoryImportError, match="identity"):
            await okx.verify_contract(client, "BTC")


@pytest.mark.asyncio
async def test_okx_advanced_archive_is_inventory_only():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_response),
    ) as client:
        result = await okx.inventory_extra_modules(
            client, asset="BTC", month="2026-05",
        )
    assert result["all_modules_imported"] is False
    assert result["modules"]["trades"]["status"] == "cataloged_not_downloaded"
    assert result["modules"]["orderbook_l2_5000"]["status"] == "cataloged_not_downloaded"
    assert result["modules"]["funding_rates"]["files"][0]["source_url"].startswith(
        "https://static.okx.com/"
    )


@pytest.mark.asyncio
async def test_okx_archive_redirect_is_never_trusted():
    def bad(req):
        if req.url.path.endswith("/market-data-history"):
            return httpx.Response(200, json={"code": "0", "data": [{
                "dateAggrType": "monthly", "details": [{
                    "instFamily": "BTC-USDT", "instType": "SWAP",
                    "groupDetails": [{
                        "filename": "BTC-USDT-SWAP-trades-2026-05.zip",
                        "url": "https://not-okx.example/bad.zip",
                    }],
                }],
            }]})
        return _response(req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
        with pytest.raises(HistoryImportError, match="host/filename"):
            await okx.inventory_extra_modules(
                client, asset="BTC", month="2026-05",
            )


def test_okx_2023_archive_start_and_unfinished_month_protection():
    assert okx.month_bounds("2023-07")[0].year == 2023
    with pytest.raises(HistoryImportError):
        okx.month_bounds("2023-06")
    with pytest.raises(HistoryImportError):
        okx.month_bounds("2026-13")

@pytest.mark.asyncio
async def test_okx_keeps_verified_month_when_another_month_and_optional_sources_fail(
    tmp_path, monkeypatch,
):
    """Optional catalog or research failures must not erase SHA-readback OHLCV."""
    from types import SimpleNamespace

    from parlay_bot.research import audit, okx_file_first
    from parlay_bot.research.free_history import _persist
    from parlay_bot.research.source_native import SOURCE

    class Drive:
        folders = SimpleNamespace(history="history", manifests="manifests")

        def __init__(self):
            self.verified = []

        async def ensure_history_file(self, **kwargs):
            return SimpleNamespace(file_id="history")

        async def ensure_manifest_file(self, **kwargs):
            return SimpleNamespace(file_id="manifest")

        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            self.verified.append((file_id, parent_id, artifact.sha256))

    async def contract(client, asset):
        assert asset == "BTC"
        return "BTC-USDT-SWAP"

    async def restore(*args, **kwargs):
        return []

    async def download_file(client, *, asset, month, root, now):
        if month == "2026-10":
            raise HistoryImportError("official archive not yet published")
        return _persist(
            source=SOURCE, key=f"okx-{asset}-USDT-SWAP-USDT-5m-{month}",
            records=[{
                "venue": "okx", "contract": "BTC-USDT-SWAP",
                "quote": "USDT", "interval_minutes": 5,
                "opened_at": f"{month}-01T00:00:00+00:00",
                "closed_at": f"{month}-01T00:04:59.999000+00:00",
                "first_observed_at": now.isoformat(),
                "open": 100, "high": 102, "low": 99, "close": 101,
                "volume_contracts": 2, "volume_base": 1,
                "volume_quote": 100, "trades": -1,
            }],
            raw_sha="f" * 64, url=okx.CANDLES, out_dir=root,
            extra={"source_integrity": okx.INTEGRITY, "venue": "okx"},
        )

    async def fallback(*args, **kwargs):
        raise HistoryImportError("historical REST gap")

    async def blocked_models(**kwargs):
        raise RuntimeError("Multiple Drive objects exist for candidate model")

    async def inventory(client, *, asset, month, module_ids):
        if module_ids == (1,):
            raise HistoryImportError("unverified OKX archive host/filename")
        label = okx.MODULES[module_ids[0]]
        return {"modules": {label: {"files": [], "status": "unavailable"}}}

    monkeypatch.setattr(okx, "verify_contract", contract)
    monkeypatch.setattr(okx, "restore_verified_source_months", restore)
    monkeypatch.setattr(audit, "audit_catalog", lambda *a, **kw: {"partitions": []})
    monkeypatch.setattr(okx_file_first, "download_file_month", download_file)
    monkeypatch.setattr(okx, "download_month", fallback)
    monkeypatch.setattr(okx, "run_native_research", blocked_models)
    monkeypatch.setattr(okx, "inventory_extra_modules", inventory)
    drive = Drive()
    result = await okx.bootstrap(
        drive=drive, client=object(), asset="BTC",
        root=tmp_path / "data", report_dir=tmp_path / "reports",
        max_new_months=2, now=OBSERVED,
    )
    assert result["newly_published_months"] == ["2026-09"]
    assert result["source_month_failures"][0]["month"] == "2026-10"
    assert result["native_status"] == "blocked_native_research"
    assert result["native_models_published"] == 0
    assert result["native_research_error"].startswith("Multiple Drive")
    assert len(result["advanced_catalog_failures"]) == 2
    assert all(row["module"] == "trades" for row in result["advanced_catalog_failures"])
    assert len(drive.verified) == 2
    inventory_report = (
        tmp_path / "reports" / "okx-extra-inventory-BTC-2026-09.json"
    ).read_text(encoding="utf-8")
    assert '"status": "unverified_catalog"' in inventory_report
