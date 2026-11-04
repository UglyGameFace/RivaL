"""Order-flow challenger: causal inputs, graded-only learning, earned confidence."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.flow_challenger import FAMILY, FLOW_FEATURES, FlowChallenger
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 4, 12, 0, 30, tzinfo=UTC)


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "flow.sqlite")
    FlowChallenger(store).initialize()
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_forward_audit (
            venue TEXT, contract TEXT, quote TEXT, horizon_minutes INTEGER,
            reference_opened_at TEXT, settled_at TEXT, outcome_up INTEGER)""")
    return store


def _bucket(conn, start, recorded, buy, sell, bid=10.0, ask=5.0):
    conn.execute(
        """INSERT INTO perp_liquidity_5m (venue,asset,bucket_start,complete,book_samples,
           spread_bps_mean,spread_bps_max,mid_last,bid_depth_10bps_mean,ask_depth_10bps_mean,
           bid_depth_25bps_mean,ask_depth_25bps_mean,depth_25bps_covered_fraction,
           buy_trades,sell_trades,buy_volume,sell_volume,first_event_at,last_event_at,
           recorded_at) VALUES ('hyperliquid','BTC',?,1,10,1.0,2.0,100,?,?,?,?,1,5,5,?,?,?,?,?)""",
        (start.isoformat(), bid, ask, bid, ask, buy, sell, start.isoformat(),
         start.isoformat(), recorded.isoformat()))


def test_inputs_never_use_buckets_recorded_after_issuance(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            _bucket(conn, NOW - timedelta(minutes=10), NOW - timedelta(minutes=5), buy=9, sell=1)
            # Recorded AFTER the forecast clock: must be invisible.
            _bucket(conn, NOW - timedelta(minutes=5), NOW + timedelta(seconds=10), buy=0, sell=10)
        flow = FlowChallenger(store).asset_inputs(NOW, {"BTC"})["BTC"]
        assert len(flow) == FLOW_FEATURES
        assert flow[0] == (9 - 1) / 10  # only the bucket known at issuance
        assert flow[-1] == 1.0  # order-flow available
        empty = FlowChallenger(store).asset_inputs(NOW, {"ETH"})["ETH"]
        assert empty == (0.0,) * FLOW_FEATURES  # missing data is flagged, not invented
    finally:
        store.close()


def test_learns_only_from_graded_outcomes_and_skips_no_trade_targets(tmp_path):
    store = _store(tmp_path)
    challenger = FlowChallenger(store)
    try:
        flow = (0.5,) + (0.0,) * (FLOW_FEATURES - 1)
        with store.connect() as conn:
            for i in range(3):
                ref = NOW - timedelta(minutes=30 - 5 * i)
                call = challenger.call(conn, venue="okx", contract="BTC-USDT-SWAP",
                                       quote="USDT", asset="BTC", horizon=5,
                                       reference_opened_at=ref, issued_at=ref,
                                       features=(0.1,) * 10, flow=flow)
                assert call["family"] == FAMILY and 0 < call["probability"] < 1
            rows = [(NOW - timedelta(minutes=30)).isoformat(), 1,
                    (NOW - timedelta(minutes=25)).isoformat(), None,   # no-trade: withdrawn
                    (NOW - timedelta(minutes=20)).isoformat(), "pending"]
            for ref, outcome in zip(rows[::2], rows[1::2], strict=True):
                settled = None if outcome == "pending" else NOW.isoformat()
                conn.execute("INSERT INTO perp_forward_audit VALUES "
                             "('okx','BTC-USDT-SWAP','USDT',5,?,?,?)",
                             (ref, settled, None if outcome == "pending" else outcome))
        assert challenger.learn(NOW) == 1  # the graded one; not withdrawn or pending
        with store.connect() as conn:
            state = conn.execute("SELECT * FROM rival_flow_model_state").fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_flow_features WHERE learned=0").fetchone()["n"]
        assert state["updates"] == 1 and state["positives"] == 1
        assert len(json.loads(state["weights"])) == 11 + FLOW_FEATURES
        assert pending == 1  # the unsettled forecast waits for its real outcome
        assert challenger.learn(NOW) == 0  # nothing is learned twice
    finally:
        store.close()


def test_young_model_stays_near_the_up_rate():
    strong = {"weights": [0.0] + [3.0] * (10 + FLOW_FEATURES), "updates": 10, "positives": 5}
    inputs = (1.0,) * (10 + FLOW_FEATURES)
    young = FlowChallenger.probability(strong, inputs)
    assert abs(young - 0.5) < 0.05  # 10 graded results earn almost no say
    seasoned = dict(strong, updates=3000, positives=1500)
    assert FlowChallenger.probability(seasoned, inputs) > 0.85
