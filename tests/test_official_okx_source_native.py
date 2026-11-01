"""Source-native OKX history is real, contiguous, and scoped to a perpetual."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research import official_okx as okx
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.learning import _rows

NOW = datetime(2026, 11, 1, tzinfo=UTC)
START = datetime(2026, 5, 1, tzinfo=UTC)


def raw(opened: datetime) -> list[str]:
    return [
        str(int(opened.timestamp() * 1000)), "100", "110", "90",
        "105", "3", "3", "315", "1",
    ]


def page(start: datetime, count: int) -> dict:
    return {"code": "0", "data": [
        raw(start + i * okx.STEP) for i in reversed(range(count))
    ]}


def test_okx_exact_page_never_accepts_wrong_or_incomplete_bars():
    last = START + 2 * okx.STEP
    rows, cursor = okx.parse_page(
        page(START, 2), asset="BTC", start=START, end=last,
        cursor=int(last.timestamp() * 1000), factor=0.1, observed=NOW,
    )
    assert len(rows) == 2
    assert cursor == int(START.timestamp() * 1000)
    assert rows[0]["venue"] == "okx"
    assert rows[0]["contract"] == "BTC-USDT-SWAP"
    assert rows[0]["volume_base"] == pytest.approx(0.3)
    assert all(row["first_observed_at"] == NOW.isoformat() for row in rows)
    bad = page(START, 2)
    bad["data"][0][8] = "0"
    with pytest.raises(HistoryImportError, match="unconfirmed"):
        okx.parse_page(
            bad, asset="BTC", start=START, end=last,
            cursor=int(last.timestamp() * 1000), factor=1, observed=NOW,
        )
    bad = page(START, 2)
    bad["data"].append(bad["data"][0])
    with pytest.raises(HistoryImportError, match="duplicated"):
        okx.parse_page(
            bad, asset="BTC", start=START, end=last,
            cursor=int(last.timestamp() * 1000), factor=1, observed=NOW,
        )


@pytest.mark.asyncio
async def test_okx_exact_full_month_is_accepted_with_real_fetch_time(
    tmp_path, monkeypatch,
):
    def exchange(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v5/market/history-candles"
        assert request.url.params["instId"] == "BTC-USDT-SWAP"
        assert request.url.params["bar"] == "5m"
        cursor = datetime.fromtimestamp(
            int(request.url.params["after"]) / 1000, UTC,
        )
        count = min(100, int((cursor - START) / okx.STEP))
        return httpx.Response(200, json=page(cursor - count * okx.STEP, count))

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(okx.asyncio, "sleep", instant_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
        artifact = await okx.fetch_month(
            client, asset="BTC", month="2026-05",
            root=tmp_path, factor=0.1, now=NOW,
        )
    meta, records = _rows(artifact.manifest_path)
    quality = audit_partition(artifact.manifest_path)
    assert artifact.records == 31 * 24 * 12
    assert quality["full_calendar_month_verified"]
    assert records[0]["opened_at"] == START.isoformat()
    assert records[-1]["opened_at"] == (
        datetime(2026, 6, 1, tzinfo=UTC) - okx.STEP
    ).isoformat()
    assert meta["source_integrity"] == okx.INTEGRITY
    assert meta["provider_signed_checksum"] is False


@pytest.mark.asyncio
async def test_missing_candle_rejected_and_not_published(tmp_path, monkeypatch):
    def exchange(request: httpx.Request) -> httpx.Response:
        cursor = datetime.fromtimestamp(
            int(request.url.params["after"]) / 1000, UTC,
        )
        count = min(100, int((cursor - START) / okx.STEP))
        result = page(cursor - count * okx.STEP, count)
        if cursor.month == 5:
            result["data"].pop()
        return httpx.Response(200, json=result)

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(okx.asyncio, "sleep", instant_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
        with pytest.raises(HistoryImportError, match="missing"):
            await okx.fetch_month(
                client, asset="BTC", month="2026-05",
                root=tmp_path, factor=0.1, now=NOW,
            )
    assert not list(tmp_path.rglob("*.manifest.json"))


def test_reject_forged_okx_origin(tmp_path):
    closed = START + okx.STEP - timedelta(milliseconds=1)
    row = {
        "venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT",
        "interval_minutes": 5, "opened_at": START.isoformat(),
        "closed_at": closed.isoformat(), "first_observed_at": NOW.isoformat(),
        "open": 100, "high": 110, "low": 90, "close": 105,
        "volume_base": 1, "trades": -1,
    }
    saved = _persist(
        source=okx.SOURCE, key="okx-BTC-USDT-SWAP-USDT-5m-2026-05",
        records=[row], raw_sha="a" * 64,
        url="https://fake.invalid/wrong", out_dir=tmp_path,
        extra={
            "venue": "okx", "contract": "BTC-USDT-SWAP",
            "quote": "USDT", "interval_minutes": 5,
            "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_integrity": okx.INTEGRITY, "source_api_bar": "5m",
            "source_api_market_type": "SWAP",
            "historical_first_observed_at_is_fetch_time": True,
            "provider_signed_checksum": False,
        },
    )
    with pytest.raises(HistoryImportError, match="ineligible"):
        _rows(saved.manifest_path)
