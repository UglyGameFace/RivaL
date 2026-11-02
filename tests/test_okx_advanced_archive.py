"""Fail-closed OKX archive bytes, schema and retrospective-feature tests."""
from __future__ import annotations

import io
import json
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.okx_advanced_archive import (
    archive_exceeds_free_transfer_budget,
    archive_url,
    automatic_archive_slot,
    ingest_one,
    inspect_archive,
    listed_auxiliary_receipts,
    official_catalog_http_400,
)


def test_auto_advanced_slot_rotates_months_and_all_native_families():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    slots = [automatic_archive_slot(now + timedelta(hours=2 * i))
             for i in range(8)]
    assert len(set(slots)) == 8
    # Early 2023+ months have only three families; 5000-level begins 2025-11.
    all_families = {
        automatic_archive_slot(now + timedelta(hours=2 * i))[1]
        for i in range(200)
    }
    assert all_families == {
        "funding_rates", "trades", "orderbook_l2_400", "orderbook_l2_5000",
    }
    assert all("2021-09" <= month <= "2026-10" for month, _ in slots)
    # November 1 must not pull an October archive before its grace period.
    early, _ = automatic_archive_slot(datetime(2026, 11, 1, tzinfo=UTC))
    assert "2021-09" <= early <= "2026-09"


def test_automatic_advanced_catalog_never_schedules_5000_level_before_launch():
    for now in (datetime(2024, 11, 4, tzinfo=UTC),
                datetime(2026, 11, 4, tzinfo=UTC)):
        for step in range(200):
            month, module = automatic_archive_slot(now + timedelta(hours=2 * step))
            assert module != "orderbook_l2_5000" or month >= "2025-11"
    with pytest.raises(ValueError, match="offset"):
        automatic_archive_slot(datetime(2026, 11, 4, tzinfo=UTC), offset=12)


def test_source_gap_candidates_are_bounded_and_disjoint():
    clock = datetime(2026, 11, 4, tzinfo=UTC)
    slots = [automatic_archive_slot(clock, offset=i) for i in range(12)]
    assert len(slots) == len(set(slots))
    assert all("2021-09" <= month <= "2026-10" for month, _ in slots)


def test_only_explicit_provider_http_400_is_skippable_catalog_rejection():
    req = httpx.Request("GET", "https://www.okx.com/api/v5/public/market-data-history")
    for status, code, expected in (
        (400, "50016", True),
        (400, "51000", False),
        (403, "50016", False),
        (429, "50016", False),
        (500, "50016", False),
    ):
        response = httpx.Response(status, request=req, json={"code": code})
        try:
            raise HistoryImportError("official OKX API request failed") from (
                httpx.HTTPStatusError("catalog request", request=req, response=response)
            )
        except HistoryImportError as exc:
            assert official_catalog_http_400(exc) is expected
    assert not official_catalog_http_400(HistoryImportError("untrusted archive"))


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
    assert rows[0]["features"] == {"trades": 2.0, "buy_size_source_units": 2.0,
                                   "sell_size_source_units": 1.0}
    assert rows[0]["available_after"] == "2026-05-03T00:05:00+00:00"
    assert (tmp_path / FILENAME).read_bytes() == data
    with pytest.raises(HistoryImportError, match="already exists"):
        async with _client(data) as client:
            await ingest_one(client=client, asset=ASSET, month=MONTH,
                             module="trades", record=ENTRY, root=tmp_path)


def test_signed_okx_catalog_version_is_allowed_for_exact_source_file():
    signed = URL + "?v=202605"
    assert archive_url(
        asset=ASSET, month=MONTH,
        record={"filename": FILENAME, "source_url": signed},
    ) == signed


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



@pytest.mark.asyncio
async def test_wrong_in_file_instrument_never_enters_training(tmp_path):
    archive = _zip(
        "trades.csv",
        f"instId,ts,side,px,sz\nETH-USDT-SWAP,{TS},buy,100,1\n",
    )
    async with _client(archive) as client:
        with pytest.raises(HistoryImportError, match="wrong swap identity"):
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


@pytest.mark.asyncio
async def test_okx_auxiliary_receipt_resume_is_only_a_bounded_name_hint():
    from types import SimpleNamespace

    existing = "BTC-USDT-SWAP-funding_rates-2023-07.zip.receipt.json"

    class Client:
        async def get(self, url, *, params, headers):
            assert url.endswith("/files")
            assert params["pageSize"] == 1000
            assert "BTC-USDT-SWAP-" in params["q"]
            return httpx.Response(200, json={"files": [
                {"id": "authentic", "name": existing, "parents": ["manifests"],
                 "appProperties": {"rival_kind": "manifest"}, "trashed": False},
                {"id": "unrelated", "name": "ETH-USDT-SWAP-trades-2024-02.zip.receipt.json",
                 "parents": ["manifests"],
                 "appProperties": {"rival_kind": "manifest"}, "trashed": False},
            ]})

    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="manifests")
        _client = Client()

        async def _verify_folder_boundary(self):
            pass

        async def _headers(self):
            return {}

        @staticmethod
        def _escape_query(value):
            return value

    assert await listed_auxiliary_receipts(Drive(), asset="BTC") == {existing}
    with pytest.raises(HistoryImportError):
        await listed_auxiliary_receipts(Drive(), asset="INVALID")


@pytest.mark.asyncio
async def test_okx_auxiliary_listing_rejects_untrusted_foreign_folder():
    from types import SimpleNamespace

    class Client:
        async def get(self, url, *, params, headers):
            return httpx.Response(200, json={"files": [
                {"id": "bad", "name": "BTC-USDT-SWAP-trades-2025-02.zip.receipt.json",
                 "parents": ["foreign"], "appProperties": {"rival_kind": "manifest"},
                 "trashed": False},
            ]})

    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="manifests")
        _client = Client()

        async def _verify_folder_boundary(self):
            pass

        async def _headers(self):
            return {}

        @staticmethod
        def _escape_query(value):
            return value

    with pytest.raises(HistoryImportError, match="untrusted"):
        await listed_auxiliary_receipts(Drive(), asset="BTC")


def test_advanced_history_family_eras_precede_five_minute_candles():
    from parlay_bot.research.official_okx_bootstrap import (
        ADVANCED_FIRST,
        advanced_month_bounds,
    )

    assert ADVANCED_FIRST == {
        1: "2021-09", 2: "2023-07", 3: "2022-03",
        4: "2023-03", 5: "2025-11",
    }
    for module, start in ADVANCED_FIRST.items():
        first, _ = advanced_month_bounds(start, module)
        assert first.strftime("%Y-%m") == start
        prior = (first - timedelta(days=1)).strftime("%Y-%m")
        with pytest.raises(HistoryImportError, match="predates"):
            advanced_month_bounds(prior, module)
    with pytest.raises(HistoryImportError, match="not completed"):
        advanced_month_bounds("2026-11", 1)

    # Official archive URL can be authenticated without moving the separate
    # strict five-minute candle bootstrap floor from July 2023.
    from parlay_bot.research.official_okx_bootstrap import month_bounds
    with pytest.raises(HistoryImportError):
        month_bounds("2022-03")


def test_okx_old_trade_and_funding_catalog_urls_are_allowed_only_for_family():
    old = "BTC-USDT-SWAP-trades-2021-09.zip"
    url = "https://static.okx.com/cdn/okex/traderecords/trades/monthly/202109/" + old
    assert archive_url(
        asset="BTC", month="2021-09", module="trades",
        record={"filename": old, "source_url": url},
    ) == url
    with pytest.raises(HistoryImportError, match="predates"):
        archive_url(
            asset="BTC", month="2021-09", module="funding_rates",
            record={"filename": old, "source_url": url},
        )


def test_two_year_okx_primary_lane_and_low_frequency_older_provenance():
    from parlay_bot.research.history_priority import window

    clock = datetime(2026, 11, 2, 12, tzinfo=UTC)
    priority = set(window(clock).completed_months)
    selected = [
        automatic_archive_slot(clock + timedelta(hours=2 * i))
        for i in range(24)
    ]
    # Only one in every 12 cycles is offered to pre-window sources.
    primary = sum(month in priority for month, _ in selected)
    assert primary >= 20
    assert any(month < "2024-11" for month, _ in selected)
    assert all(month < "2026-11" for month, _ in selected)


def test_only_actual_okx_byte_budget_excess_can_skip_to_next_original():
    for text in ("OKX archive exceeds allowed transfer",
                 "OKX archive byte limit exceeded"):
        assert archive_exceeds_free_transfer_budget(HistoryImportError(text))
    for text in ("wrong swap identity inside archive",
                 "missing/duplicate archive CSV headers",
                 "archive request not successful",
                 "private Drive checksum readback unavailable",
                 "OKX archive provider HTTP 429",
                 "no OKX events fell within the requested UTC month"):
        assert not archive_exceeds_free_transfer_budget(HistoryImportError(text))
    assert not archive_exceeds_free_transfer_budget(ValueError(
        "OKX archive exceeds allowed transfer"
    ))
