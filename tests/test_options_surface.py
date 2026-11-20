"""Deribit options-surface recording: the N1 forward sample's benchmark input.

The historical Deribit archive the N1 design wanted is paid-only, so the
surface accrues live instead (amendment 2026-11-19). The recorder keeps
the raw venue fields per instrument, skips malformed entries without
losing the snapshot, and isolates a failed currency so its outage can
never stall the other currency or the bot.
"""
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from parlay_bot.config import Settings
from parlay_bot.perpetuals.options_surface import (
    OptionsSurfaceRecorder,
    parse_instrument_name,
    surface_rows,
)
from parlay_bot.storage.hot import SQLiteHotStore

T0 = datetime(2026, 11, 20, 4, 0, 0, tzinfo=UTC)


def _entry(name, *, mark=0.032, iv=65.7, bid=0.031, ask=0.034, underlying=61982.96):
    return {
        "instrument_name": name, "mark_price": mark, "mark_iv": iv,
        "bid_price": bid, "ask_price": ask, "underlying_price": underlying,
        "open_interest": 100.0, "volume": 3.0,
    }


def _envelope(entries):
    return {"jsonrpc": "2.0", "result": entries}


def test_parse_instrument_name_reads_terms():
    terms = parse_instrument_name("BTC-20NOV26-64000-P")
    assert terms.currency == "BTC"
    assert terms.option_type == "put"
    assert terms.strike == 64000.0
    # Deribit options expire at 08:00 UTC on the expiry date.
    assert terms.expiry_at == datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    call = parse_instrument_name("ETH-25DEC26-3200.5-C")
    assert call.option_type == "call"
    assert call.strike == 3200.5


def test_parse_instrument_name_rejects_non_options():
    assert parse_instrument_name("BTC-PERPETUAL") is None
    assert parse_instrument_name("BTC-20XXX26-64000-P") is None
    assert parse_instrument_name("BTC-20NOV26-64000") is None
    assert parse_instrument_name("BTC_USDC-20NOV26-64000-C") is None
    assert parse_instrument_name(None) is None
    assert parse_instrument_name("") is None


def test_surface_rows_keep_valid_and_count_skips():
    payload = _envelope([
        _entry("BTC-20NOV26-64000-P"),
        _entry("BTC-27NOV26-70000-C", mark=0.011, iv=58.2),
        _entry("BTC-PERPETUAL"),                          # not an option name
        _entry("BTC-27NOV26-72000-C", iv="high"),         # IV not a number
        {"instrument_name": "BTC-27NOV26-74000-C"},       # no mark fields
        "not-a-dict",
    ])
    rows, skipped = surface_rows(payload, index_price=62070.46)
    assert skipped == 4
    assert len(rows) == 2
    first = rows[0]
    assert first["currency"] == "BTC"
    assert first["mark_price"] == 0.032
    assert first["mark_iv"] == 65.7
    assert first["bid_price"] == 0.031
    assert first["index_price"] == 62070.46
    assert first["expiry_at"] == datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


def test_surface_rows_reject_a_shapeless_envelope():
    with pytest.raises(TypeError):
        surface_rows({"jsonrpc": "2.0"})
    with pytest.raises(TypeError):
        surface_rows(["not", "an", "envelope"])


def test_record_and_readback_round_trip(tmp_path):
    recorder = OptionsSurfaceRecorder(SQLiteHotStore(tmp_path / "n1.sqlite"))
    rows, _ = surface_rows(_envelope([
        _entry("BTC-20NOV26-64000-P"),
        _entry("BTC-20NOV26-66000-C", mark=0.021),
    ]), index_price=62070.46)
    assert recorder.record_snapshot(rows, snapshot_at=T0) == 2
    stored = recorder.snapshots(currency="BTC", snapshot_at=T0)
    assert len(stored) == 2
    by_name = {row["instrument_name"]: row for row in stored}
    put = by_name["BTC-20NOV26-64000-P"]
    assert put["option_type"] == "put"
    assert put["strike"] == 64000.0
    assert put["expiry_at"] == "2026-11-20T08:00:00+00:00"
    assert put["snapshot_at"] == T0.isoformat()
    assert put["index_price"] == 62070.46
    assert recorder.snapshot_times(currency="BTC") == [T0.isoformat()]


def test_record_snapshot_is_idempotent_per_stamp(tmp_path):
    recorder = OptionsSurfaceRecorder(SQLiteHotStore(tmp_path / "n1.sqlite"))
    rows, _ = surface_rows(_envelope([_entry("ETH-20NOV26-3200-C")]))
    assert recorder.record_snapshot(rows, snapshot_at=T0) == 1
    # The same stamp replayed (a restarted cycle) writes nothing twice.
    assert recorder.record_snapshot(rows, snapshot_at=T0) == 0
    assert len(recorder.snapshots(currency="ETH")) == 1


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    """Fixture HTTP client: routes Deribit paths to canned payloads."""

    def __init__(self, *, books, index=None, fail_currencies=(), fail_index=False):
        self._books = books
        self._index = index or {}
        self._fail = set(fail_currencies)
        self._fail_index = fail_index

    async def get(self, url, params=None):
        params = params or {}
        if url.endswith("get_book_summary_by_currency"):
            currency = params["currency"]
            if currency in self._fail:
                raise httpx.ConnectError("venue down", request=None)
            return _Response(_envelope(self._books.get(currency, [])))
        if url.endswith("get_index_price"):
            if self._fail_index:
                raise httpx.ConnectError("index down", request=None)
            name = params["index_name"]
            return _Response({"result": {"index_price": self._index[name]}})
        raise AssertionError(f"unexpected URL {url}")


async def test_capture_records_both_currencies(tmp_path):
    recorder = OptionsSurfaceRecorder(SQLiteHotStore(tmp_path / "n1.sqlite"))
    client = _Client(
        books={
            "BTC": [_entry("BTC-20NOV26-64000-P")],
            "ETH": [_entry("ETH-20NOV26-3200-C")],
        },
        index={"btc_usd": 62070.46, "eth_usd": 3010.5},
    )
    receipt = await recorder.capture(client, now=T0)
    assert receipt["BTC"] == {"currency": "BTC", "rows": 1, "skipped": 0}
    assert receipt["ETH"]["rows"] == 1
    assert recorder.snapshots(currency="BTC")[0]["index_price"] == 62070.46
    assert recorder.snapshots(currency="ETH")[0]["index_price"] == 3010.5


async def test_capture_isolates_a_failed_currency(tmp_path):
    recorder = OptionsSurfaceRecorder(SQLiteHotStore(tmp_path / "n1.sqlite"))
    client = _Client(
        books={"BTC": [_entry("BTC-20NOV26-64000-P")]},
        index={"btc_usd": 62070.46},
        fail_currencies={"ETH"},
    )
    receipt = await recorder.capture(client, now=T0)
    assert receipt["BTC"]["rows"] == 1
    assert receipt["ETH"] == {"currency": "ETH", "error": "ConnectError"}
    assert len(recorder.snapshots(currency="BTC")) == 1
    assert recorder.snapshots(currency="ETH") == []


async def test_capture_survives_a_failed_index_call(tmp_path):
    recorder = OptionsSurfaceRecorder(SQLiteHotStore(tmp_path / "n1.sqlite"))
    client = _Client(
        books={"BTC": [_entry("BTC-20NOV26-64000-P")], "ETH": []},
        fail_index=True,
    )
    receipt = await recorder.capture(client, now=T0)
    assert receipt["BTC"]["rows"] == 1
    stored = recorder.snapshots(currency="BTC")
    assert stored[0]["index_price"] is None  # best-effort field stays NULL


def test_n1_collection_config_defaults():
    fields = Settings.model_fields
    assert fields["rival_n1_surface_enabled"].default is True
    assert fields["rival_n1_threshold_enabled"].default is True
    assert fields["rival_n1_collection_seconds"].default == 3600
    with pytest.raises(ValidationError):
        Settings(rival_n1_collection_seconds=60)  # below the 600s floor
