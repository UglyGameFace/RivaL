"""Phemex old USD inverse trade identity, no counterfeit USDT 2021 bars."""
from __future__ import annotations

import json

import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.phemex_trade_archive import minute_bounds, validate


def raw(**kwargs):
    trade = {
        "id": "2019-1-2", "timestamp": "2021-01-01T00:00:12.046Z",
        "price": "28900", "amount_quote": "1", "side": "buy",
        "type": "limit",
    }
    trade.update(kwargs)
    return json.dumps({"code": 0, "msg": "OK", "data": [trade]}).encode()


def test_verified_phemex_2021_perpetual_is_inverse_usd_not_usdt():
    start, end = minute_bounds("2021-01-01")
    report = validate(raw(), market="BTCUSD", start=start, end=end)
    assert report["market"] == "BTCUSD"
    assert report["contract_kind"] == "inverse"
    assert report["contract_quote"] == "USD"
    assert report["source_rows"] == 1
    assert len(report["raw_sha256"]) == 64
    assert not report["calendar_month_verified"]
    assert not report["historical_model_eligible"]


@pytest.mark.parametrize("item", [
    {"side": "hold"}, {"price": "NaN"}, {"amount_quote": "-4"},
    {"timestamp": "2021-01-01T00:02:12.046Z"},
    {"id": ""}, {"timestamp": "2021-01-01T00:00:00"},
])
def test_invalid_source_rows_rejected(item):
    start, end = minute_bounds("2021-01-01")
    with pytest.raises(HistoryImportError):
        validate(raw(**item), market="BTCUSD", start=start, end=end)


def test_unknown_contract_and_invalid_calendar_rejected():
    start, end = minute_bounds("2021-01-01")
    with pytest.raises(HistoryImportError):
        validate(raw(), market="BTCUSDT", start=start, end=end)
    with pytest.raises(HistoryImportError):
        minute_bounds("2020-12-31")
    with pytest.raises(HistoryImportError):
        minute_bounds("not a date")


def test_no_source_records_does_not_prove_data_unavailable():
    start, end = minute_bounds("2021-01-01")
    report = validate(b'{"code": 0, "data": []}', market="ETHUSD",
                      start=start, end=end)
    assert report["source_rows"] == 0
    assert not report["single_minute_page_complete"]
