"""Bitget v2 source-original, bounded public historical trades; never backfill depth."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.research.bitget_v2_public_trade_archive import (
    ENDPOINT,
    SCHEMA,
    _millis,
    collect_day,
    day_window,
    publish,
    validate_page,
)
from parlay_bot.research.free_history import HistoryImportError

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
START, END = day_window(NOW, 88)
CLOCK = int(NOW.timestamp() * 1000)


def source_page(rows, *, at=CLOCK, code="00000"):
    return json.dumps({
        "code": code, "requestTime": at, "data": rows,
    }, sort_keys=True).encode()


def trade(uid, asset="BTC", *, tick=START + 1000):
    return {"tradeId": str(uid), "ts": str(tick),
            "symbol": asset + "USDT", "side": "sell",
            "price": "60500", "size": "0.23"}


@pytest.mark.parametrize("days", [0, 90, -1, 500])
def test_no_pre_90_day_or_today_request(days):
    with pytest.raises(HistoryImportError):
        day_window(NOW, days)


def test_valid_utc_day_request_and_exact_full_24h():
    first, last = day_window(NOW, 89)
    assert last - first == 86_400_000
    assert datetime.fromtimestamp(first / 1000, UTC).hour == 0
    assert last < CLOCK
    with pytest.raises(HistoryImportError):
        day_window(datetime(2026, 11, 2), 88)


def test_source_page_and_cursor_are_proven():
    raw = source_page([trade(999), trade(998)])
    rows, cursor = validate_page(
        raw, asset="BTC", start=START, end=END,
        cursor=None, now_ms=CLOCK,
    )
    assert [x["tradeId"] for x in rows] == ["999", "998"]
    assert cursor is None
    with pytest.raises(HistoryImportError):
        validate_page(raw, asset="ETH", start=START, end=END,
                      cursor=None, now_ms=CLOCK)


@pytest.mark.parametrize("rows", [
    [trade(1), trade(2)],
    [trade(1), trade(1)],
    [{**trade(2), "ts": str(END + 1)}],
    [{**trade(2), "size": "-3"}],
    [{**trade(2), "side": "unknown"}],
    [{**trade(2), "price": "NaN"}],
])
def test_invalid_source_rows_cannot_be_archived(rows):
    with pytest.raises(HistoryImportError):
        validate_page(
            source_page(rows), asset="BTC", start=START, end=END,
            cursor=None, now_ms=CLOCK,
        )


def test_provider_clock_may_follow_pre_request_reference_clock():
    rows, cursor = validate_page(
        source_page([trade(22)], at=CLOCK + 750),
        asset="BTC", start=START, end=END, cursor=None, now_ms=CLOCK,
    )
    assert len(rows) == 1
    assert cursor is None
    with pytest.raises(HistoryImportError):
        validate_page(
            source_page([trade(22)], at=CLOCK + 200_000),
            asset="BTC", start=START, end=END, cursor=None, now_ms=CLOCK,
        )


def test_wrong_cursor_clock_and_provider_error_fail_closed():
    for raw, cursor in [
        (source_page([trade(99)]), "99"),
        (source_page([trade(99)]), "98"),
        (source_page([trade(99)], at=CLOCK - 500_000), None),
        (source_page([trade(99)], code="12345"), None),
    ]:
        with pytest.raises(HistoryImportError):
            validate_page(raw, asset="BTC", start=START, end=END,
                          cursor=cursor, now_ms=CLOCK)


@pytest.mark.asyncio
async def test_actual_request_params_provider_and_unverified_completeness():
    captures = []

    def respond(req):
        assert req.url.path == "/api/v2/mix/market/fills-history"
        assert req.url.host == "api.bitget.com"
        query = req.url.params
        assert query["symbol"] == "BTCUSDT"
        assert query["productType"] == "USDT-FUTURES"
        assert query["limit"] == "1000"
        assert query["startTime"] == str(START)
        assert query["endTime"] == str(END - 1)
        captures.append(dict(query))
        return httpx.Response(200, content=source_page([trade(5)]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        pages, receipt = await collect_day(
            client, asset="BTC", start=START, end=END, now=NOW,
            max_pages=2, pacer=SimpleNamespace(wait=AsyncMock()),
        )
    assert len(captures) == 1
    assert len(pages) == 1
    assert receipt["source_trades"] == 1
    assert receipt["full_trade_tape_verified"] is False
    assert receipt["full_utc_day_verified"] is False
    assert receipt["historical_depth_available_from_this_api"] is False
    assert receipt["pagination_stop_reason"] == "source_page_short_or_empty"
    assert receipt["model_training_eligible"] is False
    original = base64.b64decode(pages[0]["response_base64"])
    assert hashlib.sha256(original).hexdigest() == pages[0]["response_sha256"]


@pytest.mark.asyncio
async def test_full_api_page_keeps_verifiable_continuation_cursor():
    first = [trade(n) for n in range(3000, 2000, -1)]
    second = [trade(2000)]

    def respond(req):
        if not req.url.params.get("idLessThan"):
            return httpx.Response(200, content=source_page(first))
        assert req.url.params["idLessThan"] == "2001"
        return httpx.Response(200, content=source_page(second))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        pages, info = await collect_day(
            client, asset="BTC", start=START, end=END, now=NOW,
            max_pages=2, pacer=SimpleNamespace(wait=AsyncMock()),
        )
    assert len(pages) == 2
    assert info["source_trades"] == 1001
    assert info["pagination_stop_reason"] == "source_page_short_or_empty"
    assert info["pagination_continuation_required"] is False


@pytest.mark.asyncio
async def test_budget_truncation_explicitly_cannot_pass_as_complete():
    rows = [trade(n) for n in range(3000, 2000, -1)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=source_page(rows)),
    )) as client:
        pages, info = await collect_day(
            client, asset="BTC", start=START, end=END, now=NOW,
            max_pages=1, pacer=SimpleNamespace(wait=AsyncMock()),
        )
    assert len(pages) == 1
    assert info["pagination_continuation_required"] is True
    assert info["last_id_cursor"] == "2001"
    assert info["full_utc_day_verified"] is False


@pytest.mark.asyncio
async def test_drive_originals_and_manifest_readback(tmp_path):
    body = source_page([trade(8)])
    pages = [{
        "page": 1, "request_url": ENDPOINT, "params": {
            "symbol": "BTCUSDT", "productType": "USDT-FUTURES",
            "startTime": str(START), "endTime": str(END - 1),
            "limit": "1000",
        }, "response_sha256": hashlib.sha256(body).hexdigest(),
        "response_base64": base64.b64encode(body).decode(),
        "records": 1, "oldest_trade_id": "8", "newest_trade_id": "8",
    }]
    metadata = {
        "asset": "BTC", "utc_date":
            datetime.fromtimestamp(START / 1000, UTC).date().isoformat(),
        "source_pages": 1, "source_trades": 1,
        "full_utc_day_verified": False, "full_trade_tape_verified": False,
        "model_training_eligible": False, "live_trading_authorized": False,
    }
    drive = SimpleNamespace(
        folders=SimpleNamespace(history="history", manifests="manifests"),
        ensure_history_file=AsyncMock(return_value=SimpleNamespace(file_id="h")),
        ensure_manifest_file=AsyncMock(return_value=SimpleNamespace(file_id="m")),
        verify_downloaded_artifact=AsyncMock(),
    )
    result = await publish(drive, root=tmp_path, pages=pages, receipt=metadata)
    assert result["status"] == "source_pages_published_and_readback_verified"
    assert result["drive_sha256_readback_verified"] is True
    assert result["full_utc_day_verified"] is False
    assert drive.verify_downloaded_artifact.await_count == 2
    manifest = json.loads(next(tmp_path.glob("*.manifest.json")).read_text())
    assert manifest["schema_version"] == SCHEMA
    data = next(tmp_path.glob("*.responses.jsonl.gz")).read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["raw_sha256"]
    original = json.loads(gzip.decompress(data).splitlines()[0])
    assert base64.b64decode(original["response_base64"]) == body


def test_reject_invalid_timestamp_type():
    with pytest.raises(HistoryImportError):
        _millis(True)
