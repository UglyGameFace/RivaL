"""Official public funding original pages: no invented completeness."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import official_funding_archive as funding
from parlay_bot.research.drive_source_census import parse_page_name, summarize
from parlay_bot.research.historical_coordinator import build_plan
from parlay_bot.research.free_history import HistoryImportError


def test_months_and_source_scopes_are_independent():
    now = datetime(2026, 11, 2, tzinfo=UTC)
    assert funding.month_bounds("kucoin", "2021-01", now=now)[0].year == 2021
    assert funding.month_bounds("hyperliquid", "2023-01", now=now)[0].year == 2023
    for bad in ("2026-11", "2026-12", "2026-00", "2022-12", "../2025"):
        with pytest.raises(HistoryImportError):
            funding.month_bounds("hyperliquid", bad, now=now)
    assert len(list(funding.segments(
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC)))) == 8
    assert funding.automatic_month("kucoin", now).startswith("20")


def test_official_queries_are_contract_exact():
    start, end = funding.month_bounds(
        "kucoin", "2026-10", now=datetime(2026, 11, 1, tzinfo=UTC))
    method, url, kwargs = funding.request_spec(
        "kucoin", "BTC", start, start + timedelta(days=4))
    assert (method, url, kwargs["params"]["symbol"]) == (
        "GET", funding.URLS["kucoin"], "XBTUSDTM")
    assert kwargs["params"]["to"] < funding._ms(start + timedelta(days=4))
    method, url, kwargs = funding.request_spec(
        "hyperliquid", "XRP", start, start + timedelta(days=4))
    assert method == "POST" and kwargs["json"]["type"] == "fundingHistory"
    assert kwargs["json"]["coin"] == "XRP"


@pytest.mark.parametrize("venue", funding.VENUES)
def test_valid_original_responses_and_bounded_values(venue):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(days=4)
    ms = funding._ms(start + timedelta(hours=1))
    if venue == "hyperliquid":
        record = {"coin": "BTC", "fundingRate": "-0.0001",
                  "premium": "0.0002", "time": ms}
        wrap = lambda row: [row]
    else:
        record = {"symbol": "XBTUSDTM", "fundingRate": 0.0001,
                  "timepoint": ms}
        wrap = lambda row: {"code": "200000", "data": [row]}
    raw = json.dumps(wrap(record)).encode()
    assert funding.validate_page(
        raw, venue=venue, asset="BTC", start=start, end=end) == [ms]
    with pytest.raises(HistoryImportError, match="duplicate"):
        payload = [record, record] if venue == "hyperliquid" else {
            "code": "200000", "data": [record, record]}
        funding.validate_page(
            json.dumps(payload).encode(), venue=venue,
            asset="BTC", start=start, end=end)
    altered = dict(record)
    altered["time" if venue == "hyperliquid" else "timepoint"] = funding._ms(end)
    with pytest.raises(HistoryImportError, match="timestamp"):
        funding.validate_page(
            json.dumps(wrap(altered)).encode(), venue=venue,
            asset="BTC", start=start, end=end)
    altered = dict(record)
    altered["coin" if venue == "hyperliquid" else "symbol"] = "ETH"
    with pytest.raises(HistoryImportError, match="contract"):
        funding.validate_page(
            json.dumps(wrap(altered)).encode(), venue=venue,
            asset="BTC", start=start, end=end)
    altered = dict(record)
    altered["fundingRate"] = "NaN"
    with pytest.raises(HistoryImportError, match="magnitude"):
        funding.validate_page(
            json.dumps(wrap(altered)).encode(), venue=venue,
            asset="BTC", start=start, end=end)
    if venue == "hyperliquid":
        many = [record | {"time": ms + i} for i in range(500)]
    else:
        many = {"code": "200000", "data": [
            record | {"timepoint": ms + i} for i in range(500)]}
    with pytest.raises(HistoryImportError, match="provider-capped"):
        funding.validate_page(
            json.dumps(many).encode(), venue=venue,
            asset="BTC", start=start, end=end)


@pytest.mark.asyncio
async def test_official_http_source_and_immutable_drive_sha(tmp_path, monkeypatch):
    class Drive:
        folders = SimpleNamespace(history="history", manifests="manifests")
        saved = []
        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            self.saved.append(("source", artifact, batch_id))
            return SimpleNamespace(file_id="raw")
        async def ensure_manifest_file(self, *, artifact, batch_id):
            self.saved.append(("manifest", artifact, batch_id))
            return SimpleNamespace(file_id="manifest")
        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            assert file_id in ("raw", "manifest")
            assert parent_id in ("history", "manifests")
            assert artifact.sha256
    d = Drive()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(days=4)
    raw = json.dumps([{"coin": "ETH", "fundingRate": "-0.0002",
                       "premium": "0", "time": funding._ms(start)}]).encode()
    def handler(request):
        assert request.url.host == "api.hyperliquid.xyz"
        assert request.method == "POST"
        return httpx.Response(200, content=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        body, url = await funding.fetch_page(
            client, venue="hyperliquid", asset="ETH", start=start, end=end)
    times = funding.validate_page(
        body, venue="hyperliquid", asset="ETH", start=start, end=end)
    monkeypatch.setattr(funding, "_artifact", lambda path, count: SimpleNamespace(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), count=count))
    item = await funding.archive_page(
        d, root=tmp_path, venue="hyperliquid", asset="ETH",
        month="2026-10", part=0, start=start, end=end,
        raw=body, source_url=url, stamps=times)
    assert item["drive_original_and_manifest_readback"]
    assert len(d.saved) == 2
    names = [p.name for p in tmp_path.glob("*.manifest.json")]
    assert parse_page_name(names[0])[:4] == (
        "hyperliquid", "ETH", "funding", "2026-10")
    with pytest.raises(HistoryImportError, match="overwrite"):
        await funding.archive_page(
            d, root=tmp_path, venue="hyperliquid", asset="ETH",
            month="2026-10", part=0, start=start, end=end,
            raw=body, source_url=url, stamps=times)


def test_coordinator_sees_two_new_funding_routes_but_never_fake_months():
    report = build_plan(summarize([], now=datetime(2026, 11, 2, tzinfo=UTC)))
    for venue in funding.VENUES:
        for asset in funding.ASSETS:
            row = next(x for x in report["scopes"] if (
                x["venue"], x["asset"], x["family"]) == (venue, asset, "funding"))
            assert row["collector"] == "official-public-funding.yml"
            assert row["verified_complete_months_from_this_census"] == 0
            assert row["actual_contract_listing_month"] is None
    assert report["collector_routes_integrated"] == 180
    assert report["collection_routes_missing"] == 460
    assert report["all_history_complete"] is False
