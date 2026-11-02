"""Original Bitget liquidity source contract, freshness and private Drive proof."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.research.bitget_liquidity_archive import (
    CATEGORY,
    HOST,
    SCHEMA,
    _sha,
    collect,
    publish,
    validate,
)
from parlay_bot.research.free_history import HistoryImportError

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def payload(family: str, asset: str = "BTC", *, now_ms=NOW_MS) -> bytes:
    data = {
        "orderbook": {
            "a": [["101", "2"], ["102", "3"]],
            "b": [["100", "2"], ["99", "3"]],
            "ts": str(now_ms),
        },
        "rpi-orderbook": {
            "a": [["101", "2", "0.5"], ["102", "3", "0"]],
            "b": [["100", "2", "1"], ["99", "3", "0"]],
            "ts": str(now_ms),
        },
        "open-interest": {
            "list": [{"symbol": asset + "USDT", "openInterest": "200"}],
            "ts": str(now_ms),
        },
        "open-interest-history": {
            "list": [
                {"symbol": asset + "USDT", "openInterest": "200",
                 "oneSideOpenInterest": "100", "openInterestValue": "20100",
                 "oneSideOpenInterestValue": "10050", "timestamp": str(now_ms)}
            ],
        },
        "history-fund-rate": {
            "resultList": [
                {"symbol": asset + "USDT",
                 "fundingRate": "-0.00012",
                 "fundingRateTimestamp": str(now_ms - 3600_000)},
            ],
        },
        "fills": [
            {"execId": "a1", "side": "buy", "price": "100", "size": "2",
             "ts": str(now_ms)},
        ],
        "liquidations": {
            "list": [
                {"symbol": asset + "USDT", "side": "sell",
                 "price": "100", "amount": "1", "ts": str(now_ms)}
            ],
            "cursor": "12345",
        },
    }[family]
    return json.dumps({
        "code": "00000", "requestTime": now_ms, "data": data,
    }).encode()


@pytest.mark.parametrize("family", (
    "orderbook", "rpi-orderbook", "open-interest",
    "open-interest-history", "history-fund-rate", "fills", "liquidations",
))
def test_all_source_families_validate_without_fabricating_history(family):
    result = validate(payload(family), family=family, asset="BTC", now_ms=NOW_MS)
    assert isinstance(result, dict)
    if family.endswith("orderbook"):
        assert result["best_bid"] == "100"
        assert result["best_ask"] == "101"
        assert result["spread"] == "1"
    if family in ("fills", "liquidations", "open-interest-history"):
        assert result["records"] == 1


@pytest.mark.parametrize("family,change", [
    ("orderbook", lambda x: x["data"]["b"].__setitem__(0, ["102", "1"])),
    ("orderbook", lambda x: x["data"].__setitem__("ts", str(NOW_MS - 3600_000))),
    ("rpi-orderbook", lambda x: x["data"]["b"][0].append("bad")),
    ("open-interest", lambda x: x["data"]["list"][0].__setitem__(
        "symbol", "ETHUSDT",
    )),
    ("fills", lambda x: x["data"][0].__setitem__("size", "-1")),
    ("liquidations", lambda x: x["data"]["list"][0].__setitem__(
        "symbol", "SOLUSDT",
    )),
    ("open-interest-history", lambda x: x["data"]["list"][0].__setitem__(
        "timestamp", str(NOW_MS + 1_000_000),
    )),
    ("history-fund-rate", lambda x: x["data"]["resultList"][0].__setitem__(
        "fundingRate", "NaN",
    )),
])
def test_invalid_or_stale_source_is_never_marked_verified(family, change):
    obj = json.loads(payload(family))
    change(obj)
    with pytest.raises(HistoryImportError):
        validate(json.dumps(obj).encode(), asset="BTC", family=family, now_ms=NOW_MS)


def test_wrong_provider_error_and_unsupported_family_fail_closed():
    with pytest.raises(HistoryImportError):
        validate(b'{"code":"40001","data":{}}', asset="BTC",
                 family="orderbook", now_ms=NOW_MS)
    with pytest.raises(HistoryImportError):
        validate(payload("fills"), asset="BTC", family="invented", now_ms=NOW_MS)


@pytest.mark.asyncio
async def test_collect_live_preserves_original_provider_bytes_and_all_assets():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.bitget.com"
        asset = request.url.params["symbol"].removesuffix("USDT")
        family = request.url.path.split("/")[-1]
        assert request.url.params["category"] == CATEGORY
        if family == "liquidations":
            assert request.url.params.get("limit") == "100"
        answer = payload(family, asset)
        captured.append((asset, family, answer))
        return httpx.Response(200, content=answer)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Inject a no-delay pacer; tests never hit live Bitget.
        pacing = SimpleNamespace(wait=AsyncMock())
        originals, receipt = await collect(
            client, now=NOW, mode="live", pacer=pacing,
        )
    assert len(originals) == 24
    assert receipt["responses_verified"] == 24
    assert receipt["issues"] == []
    assert receipt["model_training_eligible"] is False
    assert receipt["live_trading_authorized"] is False
    assert receipt["historical_book_completeness_verified"] is False
    assert {x["asset"] for x in originals} == {"BTC", "ETH", "SOL", "XRP"}
    for original, (_, _, data) in zip(originals, captured):
        assert original["request_url"].startswith(HOST + "/api/v3/market/")
        assert original["response_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_partial_endpoint_failure_is_recorded_not_filled():
    def handler(request: httpx.Request) -> httpx.Response:
        asset = request.url.params["symbol"].removesuffix("USDT")
        family = request.url.path.split("/")[-1]
        if family == "rpi-orderbook" and asset == "SOL":
            return httpx.Response(403)
        return httpx.Response(200, content=payload(family, asset))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records, receipt = await collect(
            client, now=NOW, mode="live",
            pacer=SimpleNamespace(wait=AsyncMock()),
        )
    assert len(records) == 23
    assert receipt["requests_attempted"] == 24
    assert receipt["issues"][0]["asset"] == "SOL"
    assert receipt["issues"][0]["family"] == "rpi-orderbook"
    assert not any(
        r["asset"] == "SOL" and r["family"] == "rpi-orderbook"
        for r in records
    )


@pytest.mark.asyncio
async def test_funding_original_negative_rates_and_no_fake_backfill():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "100"
        asset = request.url.params["symbol"].removesuffix("USDT")
        return httpx.Response(200, content=payload("history-fund-rate", asset))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records, receipt = await collect(
            client, now=NOW, mode="funding",
            pacer=SimpleNamespace(wait=AsyncMock()),
        )
    assert len(records) == 4
    assert receipt["mode"] == "funding"
    assert receipt["historical_trades_completeness_verified"] is False
    assert records[0]["metrics"]["records"] == 1


@pytest.mark.asyncio
async def test_drive_archive_original_hashes_and_verified_readback(tmp_path):
    raw = payload("orderbook")
    records = [{
        "asset": "BTC", "family": "orderbook", "page": 1,
        "request_url": HOST + "/api/v3/market/orderbook",
        "params": {"category": CATEGORY, "symbol": "BTCUSDT",
                   "limit": "100"},
        "response_sha256": _sha(raw),
        "response_base64": __import__("base64").b64encode(raw).decode(),
        "metrics": validate(raw, asset="BTC",
                            family="orderbook", now_ms=NOW_MS),
    }]
    report = {
        "schema_version": SCHEMA, "mode": "live",
        "collected_at": NOW.isoformat(), "issues": [],
        "model_training_eligible": False, "live_trading_authorized": False,
    }
    drive = SimpleNamespace(
        folders=SimpleNamespace(history="history", manifests="manifests"),
        ensure_history_file=AsyncMock(return_value=SimpleNamespace(file_id="h")),
        ensure_manifest_file=AsyncMock(return_value=SimpleNamespace(file_id="m")),
        verify_downloaded_artifact=AsyncMock(),
    )
    result = await publish(drive, root=tmp_path, records=records, report=report)
    assert result["status"] == "verified_source_archive"
    assert result["drive_sha256_readback_verified"] is True
    assert result["model_training_eligible"] is False
    manifest = json.loads(next(tmp_path.glob("*.manifest.json")).read_text())
    archived = next(tmp_path.glob("*.responses.jsonl.gz")).read_bytes()
    assert _sha(archived) == manifest["raw_sha256"]
    original = json.loads(gzip.decompress(archived).splitlines()[0])
    assert original["response_sha256"] == _sha(raw)
    assert result["manifest_sha256"] == _sha(
        next(tmp_path.glob("*.manifest.json")).read_bytes()
    )
    assert drive.verify_downloaded_artifact.await_count == 2
    assert drive.ensure_history_file.await_args.kwargs["batch_id"].startswith(
        "bitget-liq-"
    )
