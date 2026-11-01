"""Official Bitget six-month bootstrap: exact native bytes or no publication."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research.audit import audit_partition
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows
from parlay_bot.research import official_bitget as bitget


AT = datetime(2026, 11, 1, tzinfo=UTC)


def _raw(opened: datetime) -> list[str]:
    return [
        str(int(opened.timestamp()*1000)),
        "100", "112", "90", "105", "12.5", "1250",
    ]


def _page(start: datetime, count: int) -> dict:
    return {"code": "00000", "data": [
        _raw(start + i * timedelta(minutes=5)) for i in range(count)
    ]}


def test_parse_official_page_never_invents_or_accepts_wrong_source():
    start = datetime(2026, 5, 1, tzinfo=UTC)
    finish = start + timedelta(minutes=10)
    rows = bitget.parse_history_page(
        _page(start, 2), asset="BTC", start=start, end=finish, observed=AT,
    )
    assert [x["opened_at"] for x in rows] == [
        start.isoformat(), (start + timedelta(minutes=5)).isoformat(),
    ]
    assert rows[0]["first_observed_at"] == AT.isoformat()
    assert rows[0]["venue"] == "bitget"
    assert rows[0]["contract"] == "BTCUSDT"
    assert rows[0]["trades"] == -1
    with pytest.raises(HistoryImportError, match="missing"):
        bitget.parse_history_page(
            _page(start, 1), asset="BTC", start=start,
            end=finish, observed=AT,
        )
    with pytest.raises(HistoryImportError, match="duplicate"):
        bitget.parse_history_page(
            {"code": "00000", "data": [_raw(start), _raw(start)]},
            asset="BTC", start=start, end=finish, observed=AT,
        )
    with pytest.raises(HistoryImportError, match="before candle close"):
        bitget.parse_history_page(
            _page(start, 2), asset="BTC", start=start,
            end=finish, observed=start,
        )
    with pytest.raises(HistoryImportError, match="unavailable"):
        bitget.parse_history_page(
            {"code": "99999", "data": [_raw(start), _raw(start+bitget.STEP)]},
            asset="BTC", start=start, end=finish, observed=AT,
        )


@pytest.mark.asyncio
async def test_source_exact_full_bitget_calendar_month_persists_native_artifacts(
    tmp_path, monkeypatch,
):
    start = datetime(2026, 5, 1, tzinfo=UTC)

    def fake_exchange(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/market/history-candles"
        params = request.url.params
        assert params["symbol"] == "BTCUSDT"
        assert params["category"] == "USDT-FUTURES"
        assert params["interval"] == "5m"
        assert params["type"] == "market"
        first = datetime.fromtimestamp(
            (int(params["startTime"])+1)/1000, UTC,
        )
        last = datetime.fromtimestamp(
            (int(params["endTime"])+1)/1000, UTC,
        )
        count = int((last-first)/bitget.STEP)
        assert 1 <= count <= 100
        return httpx.Response(200, json=_page(first, count))

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(bitget.asyncio, "sleep", no_wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_exchange)) as client:
        artifact = await bitget.fetch_month(
            client, asset="BTC", month="2026-05",
            root=tmp_path, now=AT,
        )
    meta, rows = _rows(artifact.manifest_path)
    audit = audit_partition(artifact.manifest_path)
    assert artifact.records == 31*24*12
    assert len(rows) == artifact.records
    assert audit["full_calendar_month_verified"]
    assert audit["coverage"] == 1
    assert rows[0]["opened_at"] == start.isoformat()
    assert rows[-1]["opened_at"] == (
        datetime(2026, 6, 1, tzinfo=UTC) - bitget.STEP
    ).isoformat()
    assert meta["source_integrity"] == bitget.INTEGRITY
    assert meta["provider_signed_checksum"] is False
    assert meta["historical_first_observed_at_is_fetch_time"] is True
    assert all(datetime.fromisoformat(r["first_observed_at"])
               > datetime.fromisoformat(r["closed_at"]) for r in rows)


@pytest.mark.asyncio
async def test_one_missing_provider_bar_aborts_without_publishing(tmp_path, monkeypatch):
    async def no_wait(_seconds: float) -> None:
        return None
    monkeypatch.setattr(bitget.asyncio, "sleep", no_wait)

    def fake_exchange(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = datetime.fromtimestamp((int(params["startTime"])+1)/1000, UTC)
        end = datetime.fromtimestamp((int(params["endTime"])+1)/1000, UTC)
        count = int((end-start)/bitget.STEP)
        rows = _page(start, count)
        if start == datetime(2026, 5, 1, tzinfo=UTC):
            rows["data"].pop()
        return httpx.Response(200, json=rows)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_exchange)) as client:
        with pytest.raises(HistoryImportError, match="missing"):
            await bitget.fetch_month(
                client, asset="BTC", month="2026-05", root=tmp_path,
                now=AT,
            )
    assert not list(tmp_path.rglob("*.manifest.json"))


def test_fake_official_bitget_manifest_origin_rejected(tmp_path):
    from parlay_bot.research.free_history import _persist
    first = datetime(2026, 5, 1, tzinfo=UTC)
    artifact = _persist(
        source=bitget.SOURCE, key="bitget-BTCUSDT-USDT-5m-2026-05",
        records=[{
            "venue": "bitget", "contract": "BTCUSDT", "quote": "USDT",
            "interval_minutes": 5, "opened_at": first.isoformat(),
            "closed_at": (first+bitget.STEP-timedelta(milliseconds=1)).isoformat(),
            "first_observed_at": AT.isoformat(), "open": 100., "high": 105.,
            "low": 95., "close": 102., "volume_base": 2., "trades": -1,
        }], raw_sha="f"*64, url="https://evil.invalid/pretend",
        out_dir=tmp_path, extra={
            "venue": "bitget", "contract": "BTCUSDT", "quote": "USDT",
            "interval_minutes": 5, "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_integrity": bitget.INTEGRITY,
            "source_api_category": "USDT-FUTURES",
            "source_api_interval": "5m", "source_api_type": "market",
            "provider_signed_checksum": False,
            "historical_first_observed_at_is_fetch_time": True,
        },
    )
    with pytest.raises(HistoryImportError, match="ineligible"):
        _rows(artifact.manifest_path)
