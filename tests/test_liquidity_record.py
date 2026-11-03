"""Live 5-minute liquidity record: parsing, bucketing, causality, storage."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.liquidity_record import (
    FLUSH_GRACE,
    LiquidityRecorder,
    LiquidityStore,
    bucket_start,
)
from parlay_bot.perpetuals.stream_quotes import StreamingBookHub
from parlay_bot.storage.database import SQLiteDatabase

T0 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _binance_depth(at: datetime, bid=100.0, ask=100.02) -> str:
    return json.dumps({"stream": "btcusdt@depth20@500ms", "data": {
        "e": "depthUpdate", "E": _ms(at), "T": _ms(at), "s": "BTCUSDT",
        "b": [[str(bid), "2"], [str(bid - 0.05), "3"], [str(bid - 0.5), "50"]],
        "a": [[str(ask), "1"], [str(ask + 0.05), "4"], [str(ask + 0.5), "60"]],
    }})


def _binance_trade(at: datetime, qty: str, buyer_is_maker: bool) -> str:
    return json.dumps({"stream": "btcusdt@aggTrade", "data": {
        "e": "aggTrade", "E": _ms(at), "T": _ms(at), "s": "BTCUSDT",
        "a": 1, "p": "100.01", "q": qty, "f": 1, "l": 1, "m": buyer_is_maker,
    }})


def test_bucket_start_is_utc_five_minute_floor():
    assert bucket_start(T0 + timedelta(minutes=7, seconds=59)) == T0 + timedelta(minutes=5)
    with pytest.raises(ValueError):
        bucket_start(datetime(2026, 11, 3, 6, 0))


def test_binance_depth_and_aggressor_flow_summarised_per_bucket():
    rec = LiquidityRecorder()
    for second in range(0, 300, 10):
        rec.ingest("binance", _binance_depth(T0 + timedelta(seconds=second)))
    rec.ingest("binance", _binance_trade(T0 + timedelta(seconds=5), "1.5", False))
    rec.ingest("binance", _binance_trade(T0 + timedelta(seconds=6), "0.5", True))
    rec.ingest("binance", _binance_trade(T0 + timedelta(seconds=7), "0.25", True))
    assert rec.drain(now=T0 + timedelta(minutes=5)) == []  # grace not yet over
    rows = rec.drain(now=T0 + timedelta(minutes=5) + FLUSH_GRACE)
    assert len(rows) == 1
    row = rows[0]
    assert (row["venue"], row["asset"], row["bucket_start"]) == (
        "binance", "BTC", T0.isoformat())
    assert row["complete"] == 1
    assert row["book_samples"] == 30
    assert row["spread_bps_mean"] == pytest.approx(0.02 / 100.01 * 10_000)
    # Within 10 bps (~0.10): best two levels each side.
    assert row["bid_depth_10bps_mean"] == pytest.approx(5)
    assert row["ask_depth_10bps_mean"] == pytest.approx(5)
    assert row["depth_25bps_covered_fraction"] == pytest.approx(1)
    assert (row["buy_trades"], row["sell_trades"]) == (1, 2)
    assert row["buy_volume"] == pytest.approx(1.5)
    assert row["sell_volume"] == pytest.approx(0.75)
    assert row["recorded_at"] == (T0 + timedelta(minutes=5) + FLUSH_GRACE).isoformat()


def test_hyperliquid_book_and_trades_route_through_hub():
    hub = StreamingBookHub(assets=("ETH",))
    hub.recorder = LiquidityRecorder(assets=("ETH",))
    at = T0 + timedelta(seconds=20)
    hub.accept("hyperliquid", json.dumps({"channel": "l2Book", "data": {
        "coin": "ETH", "time": _ms(at),
        "levels": [[{"px": "3000.0", "sz": "4", "n": 1}],
                   [{"px": "3000.6", "sz": "2", "n": 1}]],
    }}), received_at=at)
    hub.accept("hyperliquid", json.dumps({"channel": "trades", "data": [
        {"coin": "ETH", "side": "B", "px": "3000.6", "sz": "0.7", "time": _ms(at)},
        {"coin": "ETH", "side": "A", "px": "3000.0", "sz": "0.2", "time": _ms(at)},
        {"coin": "DOGE", "side": "B", "px": "1", "sz": "9", "time": _ms(at)},
    ]}), received_at=at)
    row = hub.recorder.drain(now=T0 + timedelta(minutes=10))[0]
    assert (row["venue"], row["asset"]) == ("hyperliquid", "ETH")
    assert row["complete"] == 0  # data covered only one instant of the bucket
    assert row["buy_volume"] == pytest.approx(0.7)
    assert row["sell_volume"] == pytest.approx(0.2)
    assert row["depth_25bps_covered_fraction"] == 0  # only one level per side


def test_late_frame_for_closed_bucket_is_dropped_and_garbage_counted():
    rec = LiquidityRecorder()
    rec.ingest("binance", _binance_depth(T0 + timedelta(minutes=5, seconds=1)))
    rec.ingest("binance", _binance_depth(T0 + timedelta(seconds=299)))  # older bucket
    rec.ingest("binance", "not json")
    rec.ingest("binance", json.dumps({"data": {"e": "depthUpdate", "s": "BTCUSDT"}}))
    rows = rec.drain(now=T0 + timedelta(minutes=20))
    assert [r["bucket_start"] for r in rows] == [(T0 + timedelta(minutes=5)).isoformat()]
    assert rec.rejected == 2


def test_rows_stored_once_and_never_overwritten(tmp_path):
    from types import SimpleNamespace

    db = SQLiteDatabase(tmp_path / "rival.sqlite")
    store = LiquidityStore(SimpleNamespace(connect=db.connect))
    rec = LiquidityRecorder()
    rec.ingest("binance", _binance_depth(T0))
    rows = rec.drain(now=T0 + timedelta(minutes=10))
    assert store.insert(rows) == 1
    changed = [dict(rows[0], spread_bps_mean=999.0)]
    assert store.insert(changed) == 0
    saved = store.recent(asset="BTC")
    assert len(saved) == 1 and saved[0]["spread_bps_mean"] != 999.0
