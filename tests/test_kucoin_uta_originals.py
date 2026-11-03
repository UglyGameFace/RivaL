"""KuCoin UTA V2 original source family and private ACL regressions."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import kucoin_uta_originals as k
from parlay_bot.research.drive_source_census import parse_page_name, summarize
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.historical_coordinator import build_plan


NOW = datetime(2026, 11, 3, tzinfo=UTC)


def test_kucoin_v2_attempt_floors_and_independent_product_identity():
    assert k.CONTRACTS["BTC"] == "XBTUSDTM"
    for f in ("candles_1m", "candles_15m", "candles_1h", "candles_4h",
              "mark", "index", "premium"):
        with pytest.raises(HistoryImportError):
            k.bounds("2024-12", f, now=NOW)
        assert k.bounds("2025-01", f, now=NOW)[0].year == 2025
    assert k.bounds("2021-01", "candles_1d", now=NOW)[0].year == 2021
    for bad in ("2026-11", "2026-13", "../2025", "2050-01"):
        with pytest.raises(HistoryImportError):
            k.bounds(bad, "candles_1d", now=NOW)
    assert "candles_5m" not in k.KLINES
    assert "open_interest" not in k.KLINES
    assert all(f in k.FAMILIES for f in ("mark", "index", "premium"))


def test_source_query_exact_and_no_200_row_overfetch():
    first, end = k.bounds("2026-10", "candles_1m", now=NOW)
    page = list(k.split_month(first, end, 60))[0]
    assert (page[1] - page[0]).total_seconds() == 180 * 60
    params = k.kline_params("candles_15m", "BTC", first,
                            first + timedelta(hours=45))
    assert params == {
        "tradeType": "FUTURES", "symbol": "XBTUSDTM",
        "klineType": "TRADE", "interval": "15min",
        "startAt": int(first.timestamp()),
        "endAt": int((first + timedelta(hours=45)).timestamp()) - 1,
    }
    assert k.kline_params("mark", "ETH", first, end)["klineType"] == "MARK_PRICE"
    assert k.kline_params("index", "SOL", first, end)["klineType"] == "INDEX_PRICE"
    assert k.kline_params("premium", "XRP", first, end)["klineType"] == "PREMIUM_INDEX"
    assert k.choose_family(NOW) in k.KLINES
    assert k.select_month("candles_1m", now=NOW) >= "2025-01"


def test_source_kline_validation_rejects_wrong_contract_or_fabricated_bar():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(minutes=180)
    ts = int(start.timestamp())
    bar = [ts, "10", "12", "9", "11", "8", "99"]
    def body(rows, symbol="XBTUSDTM"):
        return json.dumps({"code": "200000", "data": {
            "tradeType": "FUTURES", "symbol": symbol,
            "list": rows,
        }}).encode()
    assert k.validate_klines(
        body([bar]), family="candles_1m", asset="BTC",
        start=start, end=end) == [ts]
    for rows, error in (
        ([bar, bar], "duplicate"),
        ([[ts + 1, *bar[1:]]], "misaligned"),
        ([[ts, "10", "8", "9", "11", "8", "99"]], "OHLC"),
        ([[ts, "10", "12", "9", "11", "-1", "99"]], "market value"),
        ([[ts, "NaN", "12", "9", "11", "8", "99"]], "market value"),
        ([[ts, "10", "12", "9", "11", "8"]], "shape"),
        ([bar] * 200, "truncated"),
    ):
        with pytest.raises(HistoryImportError, match=error):
            k.validate_klines(body(rows), family="candles_1m",
                              asset="BTC", start=start, end=end)
    with pytest.raises(HistoryImportError, match="wrong"):
        k.validate_klines(body([bar], "ETHUSDTM"),
                          family="candles_1m", asset="BTC",
                          start=start, end=end)
    with pytest.raises(HistoryImportError, match="wrong"):
        k.validate_klines(
            json.dumps({"code": "200000", "data": {
                "tradeType": "SPOT", "symbol": "XBTUSDTM", "list": [bar]}}).encode(),
            family="candles_1m", asset="BTC", start=start, end=end)
    premium_bar = [ts, "-0.01", "0.03", "-0.04", "-0.02", "0", "0"]
    assert k.validate_klines(
        body([premium_bar]), family="premium", asset="BTC",
        start=start, end=end) == [ts]


def test_oi_window_conservative_retention_and_separate_source():
    hour_windows = list(k.oi_windows(interval="1hour", now=NOW))
    daily_windows = list(k.oi_windows(interval="1day", now=NOW))
    assert all((b - a) <= timedelta(days=2) for a, b in hour_windows)
    assert all((b - a) <= timedelta(days=60) for a, b in daily_windows)
    assert min(a for a, _ in hour_windows) >= NOW - timedelta(days=7)
    assert min(a for a, _ in daily_windows) >= NOW - timedelta(days=70)
    start, end = hour_windows[0]
    params = k.oi_params("BTC", "1hour", start, end)
    assert params["symbol"] == "XBTUSDTM"
    assert params["interval"] == "1hour"
    assert params["pageSize"] == 100
    assert params["endAt"] < int(end.timestamp() * 1000)
    with pytest.raises(HistoryImportError):
        k.oi_params("BTC", "15sec", start, end)
    ts = int(start.timestamp() * 1000)
    def body(rows):
        return json.dumps({"code": "200000", "data": rows}).encode()
    row = {"openInterest": "12345.6", "ts": ts}
    assert k.validate_oi(body([row]), start=start, end=end) == [ts]
    for rows, error in (
        ([row, row], "duplicated"),
        ([row | {"ts": int(end.timestamp() * 1000)}], "outside"),
        ([row | {"openInterest": "-1"}], "market value"),
        ([row] * 100, "capped"),
    ):
        with pytest.raises(HistoryImportError, match=error):
            k.validate_oi(body(rows), start=start, end=end)


@pytest.mark.asyncio
async def test_privacy_guard_rejects_public_or_unknown_drive_acl():
    def make_handler(permissions, code=200):
        def handler(request):
            assert request.url.path.endswith("/permissions")
            return httpx.Response(code, json={
                "permissions": permissions,
            })
        return handler
    for permissions, code in (
        ([{"type": "anyone", "role": "reader"}], 200),
        ([{"type": "domain", "role": "reader"}], 200),
        ([{"type": "user"}, {"type": "unknown"}], 200),
        ([{"type": "user"}], 403),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(make_handler(permissions, code))
        ) as client:
            drive = SimpleNamespace(
                DRIVE_URL="https://www.googleapis.com/drive/v3", _client=client,
                _headers=lambda: _fake_headers())
            with pytest.raises(HistoryImportError):
                await k.require_private_permissions(drive, ("private",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(
        [{"type": "user", "role": "owner"}]
    ))) as client:
        drive = SimpleNamespace(
            DRIVE_URL="https://www.googleapis.com/drive/v3", _client=client,
            _headers=lambda: _fake_headers())
        await k.require_private_permissions(drive, ("root", "history", "original"))


async def _fake_headers():
    return {}


@pytest.mark.asyncio
async def test_original_saved_private_with_sha_and_never_public(tmp_path):
    class Drive:
        folders = SimpleNamespace(history="history", manifests="manifests")
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        def __init__(self, client):
            self._client = client
            self.records = []
        async def _headers(self):
            return {}
        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            self.records.append(("source", artifact))
            return SimpleNamespace(file_id="raw")
        async def ensure_manifest_file(self, *, artifact, batch_id):
            self.records.append(("manifest", artifact))
            return SimpleNamespace(file_id="meta")
        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            assert parent_id in ("history", "manifests")
            assert artifact.sha256
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(hours=3)
    raw = json.dumps({"code": "200000", "data": {"tradeType": "FUTURES",
        "symbol": "XBTUSDTM", "list": [[int(start.timestamp()),
            "10", "12", "9", "11", "8", "99"]]}}).encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"permissions": [
            {"type": "user", "role": "owner"}]}))) as client:
        drive = Drive(client)
        receipt = await k.private_original(
            drive, root=tmp_path, asset="BTC", family="candles_1h",
            label="1hour", start=start, end=end, raw=raw,
            url=k.BASE + "/kline", stamps=[int(start.timestamp())])
        assert receipt["readback_verified"]
        assert len(drive.records) == 2
        originals = list(tmp_path.glob("*.source.json"))
        assert len(originals) == 1 and originals[0].read_bytes() == raw
        manifests = list(tmp_path.glob("*.manifest.json"))
        evidence = json.loads(manifests[0].read_text())
        assert evidence["original_sha256"] == hashlib.sha256(raw).hexdigest()
        assert evidence["public_export_permitted"] is False
        assert evidence["model_training_eligible"] is False
        assert parse_page_name(manifests[0].name)[:4] == (
            "kucoin", "BTC", "candles_1h", "2026-10")


def test_coordinator_expansion_no_full_month_claims():
    snapshot = build_plan(summarize([], now=NOW))
    assert snapshot["collector_routes_integrated"] == 224
    assert snapshot["collection_routes_missing"] == 416
    for family in k.FAMILIES:
        for asset in k.ASSETS:
            row = next(x for x in snapshot["scopes"]
                       if (x["venue"], x["asset"], x["family"]) ==
                       ("kucoin", asset, family))
            assert row["collector"] == "kucoin-uta-v2-originals.yml"
            assert row["verified_complete_months_from_this_census"] == 0
            assert row["actual_contract_listing_month"] is None
    assert snapshot["all_history_complete"] is False
