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
