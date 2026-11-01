"""No fabricated OKX minute history: catalog ZIPs, UTC edges and aggregation."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research import okx_file_first as files
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows

START = datetime(2026, 5, 1, tzinfo=UTC)
END = START + timedelta(minutes=10)
NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _zip_minute_csv(*, first: int = 0, end: int = 8, missing: int = -1,
                    name: str = "candles.csv") -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote"])
    for i in range(first, end):
        if i == missing:
            continue
        ts = int((START + timedelta(minutes=i)).timestamp() * 1000)
        writer.writerow([ts, 100 + i, 102 + i, 99 + i, 101 + i,
                         "2", "0.5", "50"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, out.getvalue().encode())
    return buffer.getvalue()


def _handler(request, *, missing: int = -1, bad_member: bool = False,
):
    if request.url.path.endswith("/market-data-history"):
        month = (datetime.fromtimestamp(
            int(request.url.params["begin"]) / 1000, UTC,
        ) + timedelta(hours=8)).strftime("%Y-%m")
        # This fixture uses a UTC calendar; production uses UTC+8 catalog
        # boundaries and receives a one-month shifted timestamp.
        url = (
            "https://static.okx.com/cdn/okex/traderecords/"
            f"candles/monthly/{month.replace('-', '')}/"
            f"BTC-USDT-SWAP-1m-{month}.zip"
        )
        return httpx.Response(200, json={"code": "0", "data": [{
            "dateAggrType": "monthly",
            "details": [{"instFamily": "BTC-USDT", "instType": "SWAP",
                         "groupDetails": [{"url": url,
                                           "filename": url.rsplit("/", 1)[-1]}]}],
        }]})
    if str(request.url).startswith("https://static.okx.com"):
        # The second downloaded archive supplies the final 2 UTC minutes.
        second = "-06.zip" in str(request.url)
        return httpx.Response(200, content=_zip_minute_csv(
            first=8 if second else 0, end=10 if second else 8,
            missing=missing, name="../escape.csv" if bad_member else "candles.csv",
        ))
    raise AssertionError("unexpected provider endpoint")


@pytest.mark.asyncio
async def test_native_okx_file_month_rebuilds_exact_five_minute_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
    ) as client:
        result = await files.download_file_month(
            client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
        )
    meta, rows = _rows(result.manifest_path)
    assert len(rows) == 2
    assert meta["source_integrity"] == files.INTEGRITY
    assert meta["provider_signed_checksum"] is False
    assert meta["source_minutes_verified"] == 10
    assert len(meta["source_archive_files"]) == 2
    assert all(len(item["sha256"]) == 64 for item in meta["source_archive_files"])
    assert rows[0]["open"] == 100
    assert rows[0]["close"] == 105
    assert rows[0]["high"] == 106
    assert rows[0]["low"] == 99
    assert rows[0]["volume_contracts"] == 10
    assert rows[0]["volume_base"] == 2.5
    assert rows[0]["volume_quote"] == 250
    assert rows[1]["opened_at"] == (START + timedelta(minutes=5)).isoformat()
    assert rows[1]["first_observed_at"] == NOW.isoformat()
    assert meta["live_trading_authorized"] is False


@pytest.mark.asyncio
async def test_missing_source_minute_refuses_native_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _handler(request, missing=6)),
    ) as client:
        with pytest.raises(HistoryImportError, match="incomplete"):
            await files.download_file_month(
                client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
            )
    assert not list(tmp_path.rglob("*.ndjson"))


@pytest.mark.asyncio
async def test_unsafe_zip_member_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _handler(request, bad_member=True)
        ),
    ) as client:
        with pytest.raises(HistoryImportError, match="unsafe"):
            await files.download_file_month(
                client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
            )


def test_unsupported_csv_schema_and_duplicate_minute_rejected():
    ts = int(START.timestamp() * 1000)
    with pytest.raises(HistoryImportError, match="schema"):
        files._read_csv(b"whatever,columns\nabc,def\n",
                        target_start_ms=ts, target_end_ms=ts + 60000, asset="BTC")
    data = (
        f"ts,o,h,l,c,vol,volCcy,volCcyQuote\n"
        f"{ts},100,102,99,101,2,0.5,50\n"
        f"{ts},100,102,99,101,2,0.5,50\n"
    ).encode()
    with pytest.raises(HistoryImportError, match="duplicate"):
        files._read_csv(data, target_start_ms=ts,
                        target_end_ms=ts + 60000, asset="BTC")


@pytest.mark.asyncio
async def test_stream_archive_429_blocks_and_preserves_fallback_signal():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(429),
    )) as client:
        with pytest.raises(HistoryImportError, match="HTTP 429"):
            await files._download_archive(
                client, url="https://static.okx.com/cdn/okex/sample.zip",
            )
