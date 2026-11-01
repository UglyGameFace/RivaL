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
