"""Original Deribit 2021 evidence cannot fake full history or live predictions."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from parlay_bot.research.deribit_trade_archive import MAX_COUNT, bounds, validate
from parlay_bot.research.free_history import HistoryImportError


def payload(*, has_more=False, contract="BTC-PERPETUAL", ts=1609459201234, ident="id1"):
    return json.dumps({"jsonrpc": "2.0", "result": {
        "has_more": has_more, "trades": [{
            "trade_id": ident, "timestamp": ts, "instrument_name": contract,
            "price": 30000.0, "amount": 10,
        }]}}).encode()


def test_2021_original_minute_is_valid_but_not_training_ready():
    start, end = bounds("2021-01-01")
    report = validate(payload(), asset="BTC", start=start, end=end)
    assert report["records"] == 1
    assert len(report["source_sha256"]) == 64
    assert not report["calendar_month_verified"]
    assert not report["historical_model_eligible"]
    assert not report["trading_changed"]


def test_window_and_date_boundaries():
    start, end = bounds("2021-01-01")
    assert start == int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
    assert end - start == 60_000
    with pytest.raises(HistoryImportError):
        bounds("2020-12-31")
    with pytest.raises(HistoryImportError):
        bounds("2021-01-01", hour=24)


@pytest.mark.parametrize("raw", [
    payload(has_more=True),
    payload(contract="ETH-PERPETUAL"),
    payload(ts=1609459260000),
    payload(ident=""),
    b"{}",
    b"not-json",
    b" " * 3_000_001,
])
def test_incomplete_wrong_contract_or_invented_rows_rejected(raw):
    start, end = bounds("2021-01-01")
    with pytest.raises(HistoryImportError):
        validate(raw, asset="BTC", start=start, end=end)
    assert MAX_COUNT <= 1000
