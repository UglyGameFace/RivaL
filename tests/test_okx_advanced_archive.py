"""Fail-closed OKX archive bytes, schema and retrospective-feature tests."""
from __future__ import annotations

import io
import json
import tarfile
import zipfile
from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.okx_advanced_archive import (
    archive_url, ingest_one, inspect_archive,
)

ASSET = "BTC"
MONTH = "2026-05"
DAY = datetime(2026, 5, 3, tzinfo=UTC)
TS = str(int(DAY.timestamp() * 1000))
FILENAME = "BTC-USDT-SWAP-trades-2026-05.zip"
URL = "https://static.okx.com/cdn/okex/traderecords/trades/monthly/202605/" + FILENAME
ENTRY = {"filename": FILENAME, "source_url": URL}


def _zip(name: str, content: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, content)
    return output.getvalue()


def _client(content: bytes, *, declared: str | None = None):
    def respond(request):
        assert str(request.url) == URL
        headers = {"content-length": declared} if declared is not None else {}
        return httpx.Response(200, content=content, headers=headers)
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
async def test_trade_archive_is_downloaded_hashed_and_research_only(tmp_path):
    data = _zip("trades.csv", (
        "ts,side,px,sz\n"
        f"{TS},buy,100,2\n"
        f"{int(TS)+1000},sell,102,1\n"
    ))
    async with _client(data) as client:
        receipt = await ingest_one(
            client=client, asset=ASSET, month=MONTH, module="trades",
            record=ENTRY, root=tmp_path,
        )
    assert receipt["parsed_rows"] == 2
    assert receipt["feature_buckets"] == 1
    assert receipt["status"] == "downloaded_schema_checked_research_only"
    assert receipt["live_model_import_allowed"] is False
    assert receipt["source_provider_checksum_verified"] is False
    assert receipt["full_month_coverage_qualified"] is False
    rows = [
        json.loads(line) for line in
        (tmp_path / (FILENAME + ".research.ndjson")).read_text().splitlines()
    ]
    assert rows[0]["features"] == {"trades": 2.0, "buy_notional": 200.0,
                                   "sell_notional": 102.0}
    assert rows[0]["available_after"] == "2026-05-03T00:05:00+00:00"
    assert (tmp_path / FILENAME).read_bytes() == data
    with pytest.raises(HistoryImportError, match="already exists"):
        async with _client(data) as client:
            await ingest_one(client=client, asset=ASSET, month=MONTH,
                             module="trades", record=ENTRY, root=tmp_path)


@pytest.mark.parametrize("url", [
    "https://evil.example/cdn/okex/" + FILENAME,
    "http://static.okx.com/cdn/okex/" + FILENAME,
    "https://static.okx.com/cdn/okex/" + FILENAME + "?token=bad",
    "https://static.okx.com/cdn/okex/BTC-USDT-SWAP-trades-2023-05.zip",
    "https://static.okx.com/cdn/okex/%2e%2e/" + FILENAME,
])
def test_source_url_must_match_exact_source_month_and_allowlist(url):
    with pytest.raises(HistoryImportError):
        archive_url(asset=ASSET, month=MONTH,
                    record={"filename": FILENAME, "source_url": url})


@pytest.mark.asyncio
async def test_transfer_budget_blocks_misleading_length(tmp_path):
    async with _client(b"unused", declared=str(33 * 1024 * 1024)) as client:
        with pytest.raises(HistoryImportError, match="transfer"):
            await ingest_one(client=client, asset=ASSET, month=MONTH,
                             module="trades", record=ENTRY, root=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_unsafe_zip_member_never_gets_published(tmp_path):
    archive = _zip("../escape.csv", f"ts,side,px,sz\n{TS},buy,100,1\n")
    async with _client(archive) as client:
        with pytest.raises(HistoryImportError, match="unsafe"):
            await ingest_one(client=client, asset=ASSET, month=MONTH,
                             module="trades", record=ENTRY, root=tmp_path)
    assert not (tmp_path / FILENAME).exists()
    assert not (tmp_path / "escape.csv").exists()


@pytest.mark.asyncio
async def test_unknown_trade_columns_fail_closed(tmp_path):
    archive = _zip("trades.csv", f"local_time,side,price,size\n{TS},buy,100,1\n")
    async with _client(archive) as client:
        with pytest.raises(HistoryImportError, match="schema"):
            await ingest_one(client=client, asset=ASSET, month=MONTH,
                             module="trades", record=ENTRY, root=tmp_path)
    assert not (tmp_path / FILENAME).exists()


def test_funding_settlement_and_l2_snapshot_are_separate_research(tmp_path):
    funding = tmp_path / "funding.zip"
    funding.write_bytes(_zip(
        "funding.csv",
        f"fundingTime,fundingRate\n{TS},-0.0001\n",
    ))
    meta = inspect_archive(funding, asset=ASSET, month=MONTH, module="funding_rates")
    assert meta["features"][0]["features"]["funding_rate"] == -0.0001
    assert meta["l2_full_book_reconstructed"] is False

    book = tmp_path / "l2.tar.gz"
    content = (
        json.dumps({"ts": int(TS), "action": "snapshot",
                    "asks": [["101", "4", "2"]], "bids": [["100", "6", "3"]]})
        + "\n"
    ).encode()
    with tarfile.open(book, mode="w:gz") as archive:
        info = tarfile.TarInfo("book.ndjson")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    derived = inspect_archive(book, asset=ASSET, month=MONTH,
                              module="orderbook_l2_400")
    assert derived["features"][0]["features"]["l2_snapshot_count"] == 1
    assert derived["l2_full_book_reconstructed"] is False
    assert derived["full_month_coverage_qualified"] is False


def test_incremental_l2_without_snapshot_not_treated_as_full_book(tmp_path):
    bad = tmp_path / "only-updates.zip"
    bad.write_bytes(_zip(
        "book.ndjson",
        json.dumps({"ts": int(TS), "action": "update",
                    "asks": [["101", "4"]], "bids": [["100", "6"]]}) + "\n",
    ))
    with pytest.raises(HistoryImportError, match="no recognizable research features"):
        inspect_archive(bad, asset=ASSET, month=MONTH,
                        module="orderbook_l2_400")
