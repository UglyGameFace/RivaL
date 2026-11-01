"""Official venue bootstrap is exact-source, bounded and fail-closed."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research import official_bitget_bootstrap as b
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows

START = datetime(2026, 5, 1, tzinfo=UTC)
OBSERVED = datetime(2026, 11, 1, tzinfo=UTC)


def _mock(request: httpx.Request, *, missing: int | None = None,
          bad_venue: bool = False) -> httpx.Response:
    assert request.url.host == "api.bitget.com"
    assert "authorization" not in request.headers
    if request.url.path.endswith("/instruments"):
        return httpx.Response(200, json={
            "code": "00000", "data": [{
                "symbol": "ETHUSDT" if bad_venue else "BTCUSDT",
                "category": "USDT-FUTURES", "type": "perpetual",
                "status": "online", "baseCoin": "BTC", "quoteCoin": "USDT",
            }],
        })
    assert request.url.path.endswith("/history-candles")
    assert request.url.params["category"] == "USDT-FUTURES"
    assert request.url.params["symbol"] == "BTCUSDT"
    assert request.url.params["interval"] == "5m"
    assert request.url.params["type"] == "market"
    start = int(request.url.params["startTime"]) + 1
    end = int(request.url.params["endTime"]) + 1
    limit = int(request.url.params["limit"])
    assert limit <= 100
    assert end-start == limit*300_000
    return httpx.Response(200, json={"code": "00000", "data": [
        [str(start+i*300_000), "101", "103", "100", "102", "5", "510"]
        for i in range(limit) if i != missing
    ]})


@pytest.mark.asyncio
async def test_official_bitget_two_page_month_produces_strict_native_manifest(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        b, "_month_bounds", lambda month: (START, START+timedelta(minutes=510)),
    )
    requests = []
    def respond(req):
        requests.append(req)
        return _mock(req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await b.verify_instrument(client, "BTC")
        record = await b.download_month(
            client, asset="BTC", month="2026-05", now=OBSERVED,
            root=tmp_path, delay=0,
        )
    assert len(requests) == 3
    meta, rows = _rows(record.manifest_path)
    assert len(rows) == 102
    assert meta["source_integrity"] == b.SOURCE_INTEGRITY
    assert meta["source_transport"] == "official_public_https"
    assert meta["provider_signed_checksum"] is False
    assert meta["retrospective"] is True
    assert meta["prospective_forecast"] is False
    assert rows[0]["venue"] == "bitget"
    assert rows[0]["contract"] == "BTCUSDT"
    assert rows[0]["opened_at"] == START.isoformat()
    assert rows[-1]["opened_at"] == (START+timedelta(minutes=505)).isoformat()
    assert all(row["first_observed_at"] == OBSERVED.isoformat() for row in rows)


@pytest.mark.asyncio
async def test_official_bitget_missing_slot_never_creates_any_archive(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        b, "_month_bounds", lambda month: (START, START+timedelta(minutes=10)),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: _mock(req, missing=1)),
    ) as client:
        with pytest.raises(HistoryImportError, match="missing source candle"):
            await b.download_month(
                client, asset="BTC", month="2026-05",
                now=OBSERVED, root=tmp_path, delay=0,
            )
    assert not list(tmp_path.rglob("*.ndjson"))


@pytest.mark.asyncio
async def test_official_bitget_wrong_instrument_rejected_before_download():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: _mock(req, bad_venue=True)),
    ) as client:
        with pytest.raises(HistoryImportError, match="instrument identity"):
            await b.verify_instrument(client, "BTC")


@pytest.mark.asyncio
async def test_official_bitget_refuses_future_or_bogus_month(
    tmp_path, monkeypatch,
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: _mock(req)),
    ) as client:
        with pytest.raises(HistoryImportError, match="not completed"):
            await b.download_month(client, asset="BTC", month="2026-11",
                                   root=tmp_path, now=OBSERVED)
        with pytest.raises(HistoryImportError):
            await b.download_month(client, asset="BTC", month="26-09",
                                   root=tmp_path, now=OBSERVED)


def test_source_integrity_does_not_accept_other_venue_forged_bitget(tmp_path, monkeypatch):
    monkeypatch.setattr(
        b, "_month_bounds", lambda month: (START, START+timedelta(minutes=5)),
    )
    row = {
        "venue": "okx", "contract": "BTCUSDT", "quote": "USDT",
        "interval_minutes": 5, "opened_at": START.isoformat(),
        "closed_at": (START+timedelta(minutes=5, milliseconds=-1)).isoformat(),
        "first_observed_at": OBSERVED.isoformat(),
        "open": 100, "high": 101, "low": 99, "close": 100,
        "volume_base": 1, "trades": -1,
    }
    from parlay_bot.research.free_history import _persist
    res = _persist(
        source=b.SOURCE, key="okx-BTCUSDT-USDT-5m-2026-05",
        records=[row], raw_sha="abc", url=b.ENDPOINT, out_dir=tmp_path,
        extra={"venue": "okx", "contract": "BTCUSDT", "quote": "USDT",
               "interval_minutes": 5, "checksum_verified": True,
               "source_integrity": b.SOURCE_INTEGRITY,
               "source_transport": "official_public_https",
               "provider_signed_checksum": False,
               "time_reference": "exchange_candle_open_not_first_observed_at"},
    )
    with pytest.raises(HistoryImportError, match="ineligible historical source"):
        _rows(res.manifest_path)
