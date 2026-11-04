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


def test_okx_and_coinbase_order_flow_is_parsed_with_correct_sides_and_units():
    import json as _json
    from datetime import UTC, datetime, timedelta

    from parlay_bot.perpetuals.liquidity_record import LiquidityRecorder

    recorder = LiquidityRecorder()
    t0 = datetime(2026, 11, 4, 13, 0, 5, tzinfo=UTC)
    ms = int(t0.timestamp() * 1000)
    recorder.ingest("okx", _json.dumps({"event": "subscribe", "arg": {"channel": "trades"}}))
    recorder.ingest("okx", _json.dumps({
        "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
        "data": [{"bids": [["100000.0", "300", "0", "4"]], "asks": [["100001.0", "100", "0", "2"]],
                  "ts": str(ms)}]}))
    recorder.ingest("okx", _json.dumps({
        "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        "data": [{"instId": "BTC-USDT-SWAP", "px": "100000", "sz": "50", "side": "buy", "ts": str(ms)},
                 {"instId": "BTC-USDT-SWAP", "px": "100000", "sz": "20", "side": "sell", "ts": str(ms)}]}))
    stamp = t0.isoformat().replace("+00:00", "Z")
    # Coinbase reports the MAKER side: a "sell" match is an aggressive BUY.
    recorder.ingest("coinbase", _json.dumps({"type": "last_match", "product_id": "BTC-USD",
                                             "side": "buy", "size": "9", "time": stamp}))
    recorder.ingest("coinbase", _json.dumps({"type": "match", "product_id": "BTC-USD",
                                             "side": "sell", "size": "2", "time": stamp}))
    recorder.ingest("coinbase", _json.dumps({"type": "ticker", "product_id": "BTC-USD",
                                             "best_bid": "99990", "best_bid_size": "1.5",
                                             "best_ask": "99991", "best_ask_size": "0.5",
                                             "time": stamp}))
    rows = {r["venue"]: r for r in recorder.drain(now=t0 + timedelta(minutes=6))}
    okx, coinbase = rows["okx"], rows["coinbase"]
    assert okx["buy_volume"] == 0.5 and okx["sell_volume"] == 0.2  # 50/20 contracts x 0.01 BTC
    assert okx["bid_depth_10bps_mean"] == 3.0 and okx["ask_depth_10bps_mean"] == 1.0
    assert coinbase["buy_volume"] == 2.0 and coinbase["sell_volume"] == 0.0  # last_match skipped
    assert coinbase["bid_depth_10bps_mean"] == 1.5 and coinbase["mid_last"] == 99990.5
    assert recorder.rejected == 0


def test_okx_liquidations_from_a_real_host_sample_are_bucketed_by_side():
    import json as _json
    from datetime import UTC, datetime, timedelta

    from parlay_bot.perpetuals.liquidity_record import LiquidityRecorder

    recorder = LiquidityRecorder()
    # Verbatim shape captured from the live host on 2026-11-04.
    sample = {"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
              "data": [{"details": [{"bkLoss": "0", "bkPx": "77662.3", "ccy": "",
                                     "posSide": "short", "side": "buy", "sz": "0.44",
                                     "ts": "1793720772160"}],
                        "instFamily": "BTC-USDT", "instId": "BTC-USDT-SWAP",
                        "instType": "SWAP", "uly": "BTC-USDT"},
                       {"details": [{"side": "sell", "posSide": "long", "sz": "3",
                                     "ts": "1793720772160"}],
                        "instId": "DOGE-USDT-SWAP"}]}  # not one of our assets
    recorder.ingest("okx", _json.dumps(sample))
    recorder.ingest("okx", _json.dumps({"event": "notice", "code": "64010"}))
    at = datetime.fromtimestamp(1793720772160 / 1000, tz=UTC)
    rows = recorder.drain(now=at + timedelta(minutes=6))
    assert len(rows) == 1 and rows[0]["asset"] == "BTC"
    assert rows[0]["short_liq_count"] == 1 and rows[0]["long_liq_count"] == 0
    assert abs(rows[0]["short_liq_volume"] - 0.0044) < 1e-12  # 0.44 contracts x 0.01 BTC
    assert recorder.rejected == 0


def test_existing_liquidity_table_gains_liquidation_columns(tmp_path):
    from parlay_bot.perpetuals.liquidity_record import LiquidityStore
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "old.sqlite")
    try:
        with store.connect() as conn:  # the table as first shipped, without liquidations
            conn.execute("""CREATE TABLE perp_liquidity_5m (venue TEXT, asset TEXT,
                bucket_start TEXT, complete INTEGER, book_samples INTEGER,
                spread_bps_mean REAL, spread_bps_max REAL, mid_last REAL,
                bid_depth_10bps_mean REAL, ask_depth_10bps_mean REAL,
                bid_depth_25bps_mean REAL, ask_depth_25bps_mean REAL,
                depth_25bps_covered_fraction REAL, buy_trades INTEGER, sell_trades INTEGER,
                buy_volume REAL, sell_volume REAL, first_event_at TEXT, last_event_at TEXT,
                recorded_at TEXT, PRIMARY KEY (venue, asset, bucket_start))""")
        LiquidityStore(store).initialize()
        with store.connect() as conn:
            names = store.database.column_names(conn, "perp_liquidity_5m")
        assert {"long_liq_volume", "short_liq_volume",
                "long_liq_count", "short_liq_count"} <= names
    finally:
        store.close()
