"""Bitget Tardis public sample day integrity, bounded retrieval and private receipts."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.research.bitget_tardis_source_archive import (
    SCHEMA,
    SOURCE,
    obtain,
    publish,
    sha,
    validate_selection,
    verify_gzip_csv,
)
from parlay_bot.research.free_history import HistoryImportError

NOW = datetime(2026, 11, 2, tzinfo=UTC)
COLUMNS = {
    "trades": ("exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
               "bitget-futures,BTCUSDT,1733011200100000,1733011200200000,1,buy,90000,0.1\n"),
    "incremental_book_L2": (
        "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\n"
        "bitget-futures,BTCUSDT,1733011200100000,1733011200200000,true,bid,90000,10\n"
        "bitget-futures,BTCUSDT,1733011200300000,1733011200400000,false,ask,90001,0\n"
    ),
}


def fixture(tmp_path, family, *, body=None):
    dest = tmp_path / "test.csv.gz"
    dest.write_bytes(gzip.compress(
        (body if body is not None else COLUMNS[family]).encode(),
        mtime=0,
    ))
    return dest


def test_exact_public_source_and_reject_old_or_fabricated():
    url = validate_selection(
        asset="BTC", family="trades", month="2024-12", now=NOW,
    )
    assert url == SOURCE + "/v1/bitget-futures/trades/2024/12/01/BTCUSDT.csv.gz"
    for month in ("2021-01", "2024-11", "2024-00", "2030-01", "../2026"):
        with pytest.raises(HistoryImportError):
            validate_selection(asset="BTC", family="trades", month=month, now=NOW)
    with pytest.raises(HistoryImportError):
        validate_selection(asset="DOGE", family="trades", month="2025-01", now=NOW)


@pytest.mark.parametrize("family,count", [("trades", 1), ("incremental_book_L2", 2)])
def test_real_original_gzip_csv_contract_and_no_false_month_completion(
    tmp_path, family, count,
):
    p = fixture(tmp_path, family)
    result = verify_gzip_csv(
        p, asset="BTC", month="2024-12", family=family, now=NOW,
    )
    assert result["source_rows"] == count
    assert result["full_historical_range_verified"] is False
    assert result["full_day_orderbook_continuity_verified"] is False
    if family == "incremental_book_L2":
        assert result["has_snapshot"] is True


@pytest.mark.parametrize("bad", [
    COLUMNS["trades"].replace("bitget-futures", "binance-futures"),
    COLUMNS["trades"].replace("BTCUSDT", "ETHUSDT"),
    COLUMNS["trades"].replace("1733011200200000", "1753011200200000"),
    COLUMNS["trades"].replace("90000,0.1", "nan,0.1"),
    COLUMNS["trades"].replace("90000,0.1", "90000,-1"),
    COLUMNS["trades"].replace(",buy,", ",wrong,"),
    "something,else\n1,2\n",
])
def test_wrong_source_and_timestamp_fails_closed(tmp_path, bad):
    path = fixture(tmp_path, "trades", body=bad)
    with pytest.raises(HistoryImportError):
        verify_gzip_csv(path, asset="BTC", month="2024-12", family="trades",
                        now=NOW)


def test_gzip_corruption_rejected(tmp_path):
    path = fixture(tmp_path, "trades")
    path.write_bytes(path.read_bytes()[:12])
    with pytest.raises(HistoryImportError):
        verify_gzip_csv(path, asset="BTC", month="2024-12", family="trades",
                        now=NOW)


@pytest.mark.asyncio
async def test_public_endpoint_raw_gzip_and_private_drive_readbacks(tmp_path):
    original = gzip.compress(COLUMNS["trades"].encode(), mtime=0)

    def route(req):
        assert req.url.host == "datasets.tardis.dev"
        assert req.url.path.endswith("/2024/12/01/BTCUSDT.csv.gz")
        return httpx.Response(
            200, content=original,
            headers={"Content-Type": "application/gzip"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
        saved, url = await obtain(
            client, asset="BTC", family="trades",
            month="2024-12", root=tmp_path, now=NOW,
        )
    assert saved.read_bytes() == original
    metrics = verify_gzip_csv(
        saved, asset="BTC", family="trades", month="2024-12", now=NOW,
    )
    drive = SimpleNamespace(
        folders=SimpleNamespace(history="history", manifests="manifests"),
        ensure_history_file=AsyncMock(return_value=SimpleNamespace(file_id="h")),
        ensure_manifest_file=AsyncMock(return_value=SimpleNamespace(file_id="m")),
        verify_downloaded_artifact=AsyncMock(),
    )
    result = await publish(
        drive, source=saved, asset="BTC", family="trades", month="2024-12",
        url=url, metrics=metrics, root=tmp_path,
    )
    assert result["status"] == "verified_private_source_day"
    assert result["drive_sha256_readback_verified"] is True
    assert result["whole_month_verified"] is False
    assert drive.verify_downloaded_artifact.await_count == 2
    proof = json.loads(next(tmp_path.glob("*.manifest.json")).read_text())
    assert proof["schema_version"] == SCHEMA
    assert proof["model_training_eligible"] is False
    assert proof["entire_month_coverage_verified"] is False
    data = next(tmp_path.glob("*.csv.gz")).read_bytes()
    assert sha(data) == result["source_sha256"]
    assert hashlib.sha256(data).hexdigest() == proof["original_sha256"]


@pytest.mark.asyncio
async def test_redirect_or_access_restriction_does_not_bypass_site(tmp_path):
    for code in (302, 403, 451):
        def route(_, status=code):
            return httpx.Response(
                status, headers={"Location": "https://not-tardis.example/data"},
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            with pytest.raises(HistoryImportError):
                await obtain(
                    client, asset="BTC", family="trades",
                    month="2024-12", root=tmp_path, now=NOW,
                )
    assert not list(tmp_path.glob("*.part"))
