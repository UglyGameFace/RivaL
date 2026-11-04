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


def test_v2_adds_okx_coinbase_flow_and_spot_basis_beside_v1(tmp_path):
    from parlay_bot.perpetuals.flow_challenger import FlowChallengerV2

    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            start = NOW - timedelta(minutes=10)
            for venue, mid, buy, sell in (("hyperliquid", 100.10, 6, 4),
                                          ("okx", 100.05, 3, 1), ("coinbase", 100.00, 1, 3)):
                conn.execute(
                    """INSERT INTO perp_liquidity_5m (venue,asset,bucket_start,complete,
                       book_samples,spread_bps_mean,spread_bps_max,mid_last,
                       bid_depth_10bps_mean,ask_depth_10bps_mean,bid_depth_25bps_mean,
                       ask_depth_25bps_mean,depth_25bps_covered_fraction,buy_trades,
                       sell_trades,buy_volume,sell_volume,first_event_at,last_event_at,
                       recorded_at) VALUES (?, 'BTC', ?, 1, 5, 1, 2, ?, 2, 2, 2, 2, 1, 1, 1,
                       ?, ?, ?, ?, ?)""",
                    (venue, start.isoformat(), mid, buy, sell, start.isoformat(),
                     start.isoformat(), (NOW - timedelta(minutes=4)).isoformat()))
        v2 = FlowChallengerV2(store)
        vector = v2.asset_inputs(NOW, {"BTC"})["BTC"]
        assert len(vector) == v2.n_flow == FLOW_FEATURES + 7
        okx_flow, okx_book, spot_flow, spot_book, basis, has_okx, has_spot = vector[-7:]
        assert okx_book == spot_book == 0.0  # balanced 2 vs 2 books
        assert okx_flow == 0.5 and spot_flow == -0.5 and has_okx == has_spot == 1.0
        assert abs(basis - (100.10 / 100.00 - 1) * 1e4 / 5) < 1e-9  # perp over spot, scaled
        with store.connect() as conn:
            call = v2.call(conn, venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
                           asset="BTC", horizon=5, reference_opened_at=NOW, issued_at=NOW,
                           features=(0.0,) * 10, flow=vector)
            v1_rows = conn.execute("SELECT COUNT(*) AS n FROM rival_flow_features").fetchone()["n"]
        assert call["family"] == "flow_v2" and v1_rows == 0  # separate tables and model
        with store.connect() as conn:  # v1 refuses a v2-length vector instead of mis-reading it
            assert FlowChallenger(store).call(
                conn, venue="okx", contract="BTC-USDT-SWAP", quote="USDT", asset="BTC",
                horizon=5, reference_opened_at=NOW, issued_at=NOW,
                features=(0.0,) * 10, flow=vector) is None
    finally:
        store.close()


def test_model_state_is_read_once_per_cycle_and_refreshed_after_learning(tmp_path):
    store = _store(tmp_path)
    challenger = FlowChallenger(store)
    reads = []
    real = challenger._state
    challenger._state = lambda conn, a, h: reads.append((a, h)) or real(conn, a, h)
    try:
        flow = (0.0,) * FLOW_FEATURES
        with store.connect() as conn:
            for i in range(3):
                ref = NOW - timedelta(minutes=30 - 5 * i)
                challenger.call(conn, venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
                                asset="BTC", horizon=5, reference_opened_at=ref,
                                issued_at=ref, features=(0.1,) * 10, flow=flow)
            conn.execute("INSERT INTO perp_forward_audit VALUES "
                         "('okx','BTC-USDT-SWAP','USDT',5,?,?,1)",
                         ((NOW - timedelta(minutes=30)).isoformat(), NOW.isoformat()))
        assert reads == [("BTC", 5)]  # three forecasts, one read
        challenger.learn(NOW)
        with store.connect() as conn:
            after = challenger.call(conn, venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
                                    asset="BTC", horizon=5, reference_opened_at=NOW,
                                    issued_at=NOW, features=(0.1,) * 10, flow=flow)
        assert after["training_rows"] == 1  # learned weights, not the cached copy
    finally:
        store.close()
