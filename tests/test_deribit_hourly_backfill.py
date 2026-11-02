"""Deribit overflow splits may not silently drop timestamped original trades."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from parlay_bot.research.deribit_hourly_backfill import (
    MAX_REQUESTS,
    hour_range,
    inspect_page,
    stable_partition,
)
from parlay_bot.research.free_history import HistoryImportError

START = int(datetime(2021, 1, 2, tzinfo=UTC).timestamp() * 1000)
END = START + 3_600_000


def payload(records, *, more=False):
    return json.dumps({"jsonrpc": "2.0", "result": {
        "has_more": more, "trades": records,
    }}).encode()


def trade(ts, suffix=1, *, instrument="BTC-PERPETUAL", price=30000):
    return {"timestamp": ts, "trade_id": f"BTC-PERPETUAL-{suffix}",
            "instrument_name": instrument, "price": price, "amount": 10}


def test_source_page_is_verified_without_claiming_whole_month():
    raw = payload([trade(START + 1)])
    rows, more = inspect_page(raw, asset="BTC",
                              start_ms=START, end_ms=END)
    assert len(rows) == 1 and not more
    receipt = stable_partition(asset="BTC", start_ms=START, end_ms=END,
                               raw=raw, source_url="https://history.deribit.com/...",
                               rows=len(rows))
    assert receipt["source_chunk_complete"] is True
    assert receipt["whole_calendar_month_verified"] is False
    assert receipt["live_model_eligible"] is False
    assert len(receipt["source_sha256"]) == 64


def test_real_historical_utc_hour_identity_and_bounds():
    a, b = hour_range("2021-01-02", 0)
    assert a == START and b - a == 3600000
    with pytest.raises(HistoryImportError):
        hour_range("2020-12-31", 0)
    with pytest.raises(HistoryImportError):
        hour_range("2021-01-02", 24)
    assert 0 < MAX_REQUESTS <= 100


@pytest.mark.parametrize("raw", [
    payload([trade(START + 10, instrument="ETH-PERPETUAL")]),
    payload([trade(START + 10, price=float("nan"))]),
    payload([trade(START - 1)]),
    payload([trade(END)]),
    payload([trade(START + 200), trade(START + 100, 2)]),
    payload([trade(START + 10), trade(START + 11)]),
    b"not-json",
    b"{}",
    b" " * 9_000_001,
])
def test_reject_misaligned_duplicate_foreign_trades(raw):
    with pytest.raises(HistoryImportError):
        inspect_page(raw, asset="BTC", start_ms=START, end_ms=END)


def test_more_flag_is_never_reported_complete_by_source_decoder():
    rows, more = inspect_page(
        payload([trade(START + 1)], more=True), asset="BTC",
        start_ms=START, end_ms=END)
    assert more is True and len(rows) == 1


@pytest.mark.asyncio
async def test_time_split_exactly_accounts_for_each_subwindow(monkeypatch, tmp_path):
    import parlay_bot.research.deribit_hourly_backfill as job

    calls = []

    async def fake_query(client, *, asset, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        if end_ms - start_ms > 1_800_000:
            return payload([trade(start_ms + 1)], more=True), "https://history.deribit.com"
        return payload([trade(start_ms + 5, len(calls))]), "https://history.deribit.com"

    async def fake_archive(drive, *, root, meta, raw):
        return {"start_ms": meta["start_ms"], "end_ms": meta["end_ms_exclusive"],
                "trades": meta["native_trade_rows"], "readback_verified": True}

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(job, "query", fake_query)
    monkeypatch.setattr(job, "archive_chunk", fake_archive)
    monkeypatch.setattr(job.asyncio, "sleep", no_sleep)
    result = await job.collect_hour(None, None, asset="BTC", start_ms=START,
                                    end_ms=END, root=tmp_path)
    assert result["hour_fully_accounted_for"]
    assert result["verified_trades"] == 2
    assert calls == [(START, END), (START, START + 1_800_000),
                     (START + 1_800_000, END)]
