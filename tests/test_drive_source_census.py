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
    # Names alone never certify a scope: no byte was re-read in this census.
    assert btc["name_inventory_complete"] is False
    assert btc["bytes_reverified_in_this_census"] is False
    assert btc["byte_readback_months_verified"] == []
    assert report["out_of_supported_month_range"] == 1
    # The same complete listing WITH a byte readback of that month certifies.
    verified = census.summarize(
        names, now=datetime(2023, 8, 1, tzinfo=UTC),
        byte_readback={"verified": {("okx", "BTC", "candles_5m", "2023-07")}},
    )
    btc = next(row for row in verified["month_scopes"]
               if row["venue"] == "okx" and row["asset"] == "BTC")
    assert btc["name_inventory_complete"] is True
    assert btc["bytes_reverified_in_this_census"] is True
    assert btc["byte_readback_months_verified"] == ["2023-07"]


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


def test_source_native_older_name_index_is_not_a_cross_venue_listing_claim():
    months = ["2021-01", "2021-02", "2021-03"]
    names = ["kucoin-XBTUSDTM-USDT-5m-" + m + "--" + "e" * 16 +
             ".manifest.json" for m in months]
    report = census.summarize(names, now=NOW)
    matches = [r for r in report["all_completed_scope_name_hints"]
               if r["venue"] == "kucoin" and r["asset"] == "BTC"]
    assert len(matches) == 1
    assert matches[0]["listed_completed_utc_months"] == months
    assert not matches[0]["byte_readbacks_verified_in_this_census"]
    assert not matches[0]["contract_listing_verified"]
    assert not any(r["venue"] == "bitget"
                   for r in report["all_completed_scope_name_hints"])


def test_readback_sample_is_bounded_deterministic_and_spread():
    names = [f"BTCUSDT-5m-2021-{m:02d}--" + "a" * 16 + ".manifest.json"
             for m in range(1, 21)]
    first = census.choose_readback_sample(names, sample_size=4)
    assert len(first) == 4
    assert first == sorted(first)
    assert first == census.choose_readback_sample(names, sample_size=4)
    assert first == [names[0], names[5], names[10], names[15]]
    assert census.choose_readback_sample(names[:5], sample_size=8) == names[:5]
    assert census.choose_readback_sample(["not-a-manifest"], sample_size=8) == []
    with pytest.raises(ValueError):
        census.choose_readback_sample(names, sample_size=0)
    with pytest.raises(ValueError):
        census.choose_readback_sample(names, sample_size=17)


def test_summarize_ignores_readback_for_unlisted_months():
    report = census.summarize(
        ["BTCUSDT-5m-2021-01--" + "a" * 16 + ".manifest.json"], now=NOW,
        byte_readback={"verified": {("binance", "BTC", "candles_5m", "2021-02")}},
    )
    btc = next(x for x in report["month_scopes"]
               if x["venue"] == "binance" and x["asset"] == "BTC")
    assert btc["bytes_reverified_in_this_census"] is False
    assert btc["byte_readback_months_verified"] == []
    assert btc["name_inventory_complete"] is False


class _FakeReadbackStream:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.payload


class _FakeReadbackClient:
    """Minimal Drive client: exact-name manifest lookup + media download."""

    def __init__(self, manifests):
        self.manifests = manifests

    async def get(self, url, *, params, headers, follow_redirects=False):
        import re

        match = re.search(r"name = '([^']+)'", params.get("q", ""))
        name = match.group(1) if match else ""
        payload = self.manifests.get(name)
        if payload is None:
            return httpx.Response(200, json={"files": []})
        import hashlib

        return httpx.Response(200, json={"files": [{
            "id": "file-1", "name": name, "size": len(payload),
            "parents": ["trusted"], "trashed": False,
            "appProperties": {
                "rival_kind": "manifest",
                "rival_sha256": hashlib.sha256(payload).hexdigest(),
            },
        }]})

    def stream(self, method, url, *, params, headers, follow_redirects=False):
        file_id = url.rsplit("/", 1)[-1]
        assert file_id == "file-1"
        payload = next(iter(self.manifests.values()))
        return _FakeReadbackStream(payload)


class _FakeReadbackDrive:
    DRIVE_URL = "https://www.googleapis.com/drive/v3"
    folders = SimpleNamespace(manifests="trusted")

    def __init__(self, manifests):
        self._client = _FakeReadbackClient(manifests)

    async def _verify_folder_boundary(self):
        pass

    async def _headers(self):
        return {}

    @staticmethod
    def _escape_query(s):
        return s


def _manifest_bytes(*, month, short, data_sha="d" * 64, source_sha="e" * 64):
    import json

    info = {
        "venue": "binance", "contract": "BTCUSDT", "quote": "USDT",
        "interval_minutes": 5, "data_sha256": data_sha,
        "source_sha256": source_sha,
        "file": f"BTCUSDT-5m-{month}--{short}.ndjson",
    }
    return (json.dumps(info, sort_keys=True) + "\n").encode()


@pytest.mark.asyncio
async def test_sampled_byte_readback_verifies_only_what_it_read():
    # The fake client serves one manifest payload for every media download,
    # so keep exactly one good manifest in the store and drive the failure
    # cases through names the lookup cannot resolve or content that fails
    # the name/content tie. A bad-hash manifest is served via a second
    # drive whose single stored payload is the bad one.
    good_name = "BTCUSDT-5m-2021-01--" + "a" * 16 + ".manifest.json"
    good = _FakeReadbackDrive({good_name: _manifest_bytes(month="2021-01", short="a" * 16)})
    result = await census.sampled_byte_readback(
        good, [good_name, "not-a-manifest.json"], sample_size=8,
    )
    assert result["sampled"] == [good_name]
    assert result["verified"] == {("binance", "BTC", "candles_5m", "2021-01")}
    assert result["failed"] == []
    report = census.summarize([good_name], now=NOW, byte_readback=result)
    btc = next(x for x in report["month_scopes"]
               if x["venue"] == "binance" and x["asset"] == "BTC")
    assert btc["bytes_reverified_in_this_census"] is True
    assert btc["byte_readback_months_verified"] == ["2021-01"]
    assert btc["name_inventory_complete"] is False  # most months unlisted

    missing_name = "BTCUSDT-5m-2021-02--" + "b" * 16 + ".manifest.json"
    result = await census.sampled_byte_readback(
        good, [missing_name], sample_size=8,
    )
    assert result["verified"] == set()
    assert [f["manifest_name"] for f in result["failed"]] == [missing_name]

    bad_name = "BTCUSDT-5m-2021-03--" + "c" * 16 + ".manifest.json"
    bad = _FakeReadbackDrive({
        bad_name: _manifest_bytes(
            month="2021-03", short="c" * 16, data_sha="not-a-hash",
            source_sha=None,
        ),
    })
    result = await census.sampled_byte_readback(bad, [bad_name], sample_size=8)
    assert result["verified"] == set()
    assert [f["manifest_name"] for f in result["failed"]] == [bad_name]
    assert "no valid content hash" in result["failed"][0]["reason"]
