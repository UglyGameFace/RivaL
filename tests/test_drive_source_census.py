"""Census counts are cumulative remote hints, not qualified source months."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import drive_source_census as census

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def test_exact_month_name_and_source_family_parsing():
    assert census.parse_name(
        "BTCUSDT-5m-2021-01--" + "a"*16 + ".manifest.json"
    ) == ("binance", "BTC", "candles_5m", "2021-01", "a"*16)
    assert census.parse_name(
        "okx-XRP-USDT-SWAP-USDT-5m-2025-12--" + "b"*16 + ".manifest.json"
    ) == ("okx", "XRP", "candles_5m", "2025-12", "b"*16)
    assert census.parse_name(
        "gatebulk-futures_usdt-ETH_USDT-funding_applies-2023-03--"
        + "c"*16 + ".manifest.json"
    ) == ("gate", "ETH", "funding_applies", "2023-03", "c"*16)
    assert census.parse_name("not-a-source.manifest.json") is None


def test_name_coverage_never_certifies_training_or_missing_bytes():
    names = [
        "BTCUSDT-5m-2021-01--" + "a"*16 + ".manifest.json",
        "BTCUSDT-5m-2021-01--" + "b"*16 + ".manifest.json",
        "gatebulk-futures_usdt-SOL_USDT-candlesticks_5m-2023-04--"
        + "c"*16 + ".manifest.json",
        "okx-XRP-USDT-SWAP-USDT-5m-2026-11--" + "d"*16 + ".manifest.json",
    ]
    report = census.summarize(names, now=NOW)
    btc = next(x for x in report["month_scopes"]
               if x["venue"] == "binance" and x["asset"] == "BTC")
    assert btc["listed_manifest_months"] == 1
    assert btc["not_listed_months"] == btc["expected_completed_months"] - 1
    assert btc["conflicting_months"] == ["2021-01"]
    assert btc["name_inventory_complete"] is False
    assert btc["bytes_reverified_in_this_census"] is False
    assert btc["model_training_qualified"] is False
    assert report["out_of_supported_month_range"] == 1
    assert report["all_source_families_2021_to_present_verified"] is False


@pytest.mark.asyncio
async def test_drive_pagination_checks_trusted_identity_and_full_completion():
    class Client:
        calls = 0
        async def get(self, url, *, params, headers):
            self.calls += 1
            assert params["pageToken"] == "page2" if self.calls == 2 else "pageToken" not in params
            return httpx.Response(200, json={
                "files": [{
                    "id": str(self.calls),
                    "name": "BTCUSDT-5m-2021-01--" + "a"*16 + ".manifest.json",
                    "parents": ["trusted"], "trashed": False,
                    "appProperties": {"rival_kind": "manifest"},
                }],
                **({"nextPageToken": "page2"} if self.calls == 1 else {}),
            })
    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="trusted")
        _client = Client()
        async def _verify_folder_boundary(self):
            pass
        async def _headers(self):
            return {}
        @staticmethod
        def _escape_query(s):
            return s
    assert len(await census.list_names(Drive())) == 2


@pytest.mark.asyncio
async def test_unavailable_drive_cannot_be_reported_as_complete():
    class Client:
        async def get(self, url, *, params, headers):
            return httpx.Response(503)
    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="trusted")
        _client = Client()
        async def _verify_folder_boundary(self):
            pass
        async def _headers(self):
            return {}
        @staticmethod
        def _escape_query(s):
            return s
    with pytest.raises(census.HistoryImportError, match="unavailable"):
        await census.list_names(Drive())



def test_okx_census_uses_actual_official_first_supported_month():
    from parlay_bot.research.official_okx_bootstrap import FIRST

    assert census.START_MONTH["okx"] == FIRST == "2023-07"
    names = [
        "okx-BTC-USDT-SWAP-USDT-5m-2023-07--" + "a"*16 + ".manifest.json",
        "okx-BTC-USDT-SWAP-USDT-5m-2023-06--" + "b"*16 + ".manifest.json",
    ]
    report = census.summarize(names, now=datetime(2023, 8, 1, tzinfo=UTC))
    btc = next(row for row in report["month_scopes"]
               if row["venue"] == "okx" and row["asset"] == "BTC")
    assert btc["source_first_month"] == "2023-07"
    assert btc["expected_completed_months"] == 1
    assert btc["listed_manifest_months"] == 1
    assert btc["not_listed_months"] == 0
    assert btc["name_inventory_complete"] is True
    assert btc["bytes_reverified_in_this_census"] is False
    assert report["out_of_supported_month_range"] == 1


def test_bitget_all_four_source_families_remain_separate_and_unqualified():
    names = [
        "bitget-v3-BTC-market-2025-12--" + "a"*16 + ".manifest.json",
        "bitget-v3-BTC-mark-2025-12--" + "b"*16 + ".manifest.json",
        "bitget-v3-BTC-index-2025-12--" + "c"*16 + ".manifest.json",
        "bitget-v3-BTC-premium-2025-12--" + "d"*16 + ".manifest.json",
    ]
    report = census.summarize(names, now=NOW)
    scopes = [row for row in report["month_scopes"]
              if row["venue"] == "bitget" and row["asset"] == "BTC"]
    assert {row["family"] for row in scopes} == {"market", "mark", "index", "premium"}
    assert len(scopes) == 4
    assert all(row["listed_manifest_months"] == 1 for row in scopes)
    assert all(row["bytes_reverified_in_this_census"] is False for row in scopes)
    assert all(row["model_training_qualified"] is False for row in scopes)
    assert all(row["start_month_is_attempt_floor_not_listing_proof"] for row in scopes)
    assert report["all_source_families_2021_to_present_verified"] is False
    assert census.parse_name("bitget-v3-BTC-invalid-2025-12--" + "d"*16 +
                             ".manifest.json") is None


def test_partial_lighter_and_dydx_pages_not_misrepresented_as_full_months():
    names = [
        "dydx-v4-SOL-USD-funding-2025-06-00-" + "a"*16 + ".manifest.json",
        "dydx-v4-SOL-USD-funding-2025-06-01-" + "b"*16 + ".manifest.json",
        "lighter-XRP-mark_price-2025-06-00-" + "c"*16 + ".manifest.json",
        "lighter-XRP-funding-2025-06-00-" + "d"*16 + ".manifest.json",
    ]
    report = census.summarize(names, now=NOW)
    partial = report["page_level_sources_not_full_months"]
    assert len(partial) == 3
    funding = next(p for p in partial if p["venue"] == "dydx")
    assert funding["listed_page_numbers"] == 2
    assert funding["family"] == "funding_with_oracle_price"
    assert all(p["full_calendar_month_verified"] is False for p in partial)
    assert all(p["model_training_qualified"] is False for p in partial)
    assert not any(p["venue"] in ("dydx", "lighter")
                   for p in report["month_scopes"])
    assert report["unrecognized_manifest_names"] == 0


def test_identical_remote_names_and_conflicting_original_pages_are_visible():
    duplicate = "bitget-v3-ETH-market-2025-08--" + "a"*16 + ".manifest.json"
    first = "lighter-ETH-mark_price-2025-07-00-" + "b"*16 + ".manifest.json"
    second = "lighter-ETH-mark_price-2025-07-00-" + "c"*16 + ".manifest.json"
    report = census.summarize([duplicate, duplicate, first, second], now=NOW)
    scope = next(r for r in report["month_scopes"]
                 if r["venue"] == "bitget" and r["asset"] == "ETH"
                 and r["family"] == "market")
    assert scope["duplicate_name_months"] == ["2025-08"]
    assert scope["name_inventory_complete"] is False
    assert report["duplicate_manifest_names"] == 1
    partial = next(p for p in report["page_level_sources_not_full_months"]
                   if p["venue"] == "lighter")
    assert partial["conflicting_page_numbers"] == [0]
    assert partial["full_calendar_month_verified"] is False



def test_okx_advanced_receipts_are_visible_without_falsely_qualifying_l2():
    authentic = [
        "BTC-USDT-SWAP-funding_rates-2022-03.zip.receipt.json",
        "BTC-USDT-SWAP-trades-2021-09.zip.receipt.json",
        "SOL-USDT-SWAP-book-2025-11.tar.gz.receipt.json",
    ]
    report = census.summarize(authentic, now=NOW)
    hints = report["okx_auxiliary_receipt_name_hints"]
    assert len(hints) == 3
    assert {(h["asset"], h["utc_month"]) for h in hints} == {
        ("BTC", "2022-03"), ("BTC", "2021-09"), ("SOL", "2025-11"),
    }
    assert report["unrecognized_manifest_names"] == 0
    assert all(h["source_modules_inferred_from_filename"] is False for h in hints)
    assert all(h["bytes_reverified_in_this_census"] is False for h in hints)
    assert all(h["full_source_month_verified"] is False for h in hints)
    assert report["all_source_families_2021_to_present_verified"] is False


def test_true_gate_hourly_l2_names_are_not_promoted_to_month_complete():
    names = [
        "gate-hourly-l2-BTC-orderbooks_slice-2023042505--"
        + "a" * 16 + ".gz.manifest.json",
        "gate-hourly-l2-BTC-orderbooks_slice-2023042505--"
        + "b" * 16 + ".gz.manifest.json",
        "gate-hourly-l2-ETH-orderbooks-2023042505--"
        + "c" * 16 + ".gz.manifest.json",
        "gate-hourly-l2-BTC-orderbooks_slice-2023042506--"
        + "d" * 16 + ".gz.manifest.json",
    ]
    report = census.summarize(names, now=NOW)
    scopes = report["gate_hourly_l2_not_full_months"]
    assert len(scopes) == 2
    btc = next(s for s in scopes if s["asset"] == "BTC")
    assert btc["utc_month"] == "2023-04"
    assert btc["distinct_named_hours"] == 2
    assert btc["hour_sha_conflicts"] == ["2023042505"]
    assert btc["bytes_reverified_in_this_census"] is False
    assert btc["full_month_source_coverage_verified"] is False
    assert btc["model_training_qualified"] is False
    assert report["all_source_families_2021_to_present_verified"] is False
    assert report["unrecognized_manifest_names"] == 0
