"""flow_v3 challenger: causal inputs, graded-only learning, earned confidence.

Mirrors tests/test_flow_challenger.py, plus the v3 additions: causal
standardization from graded data only, the all-flags-zero skip rule,
liquidations (OKX-only), Platt calibration gating (ECE + top bucket),
the selected() artifact-identity check, BLOCKED_FAMILIES, and promotion
receipts surfaced through the status card.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.family_governance import BLOCKED_FAMILIES, FamilyPromotion
from parlay_bot.perpetuals.flow_challenger_v3 import (
    FAMILY,
    FEATURE_DIGEST,
    N_FLOW,
    FlowChallengerV3,
    calibration_gate,
    fit_platt,
)
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import (
    ForwardAccuracyMonitor,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.promotion_receipts import record_promotion_receipt
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 4, 12, 0, 30, tzinfo=UTC)


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "flow3.sqlite")
    FlowChallengerV3(store).initialize()
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_forward_audit (
            venue TEXT, contract TEXT, quote TEXT, horizon_minutes INTEGER,
            reference_opened_at TEXT, settled_at TEXT, outcome_up INTEGER)""")
    return store


def _bucket(conn, venue, start, recorded, buy, sell, bid=10.0, ask=5.0,
            long_liq=0.0, short_liq=0.0, spread=1.0):
    conn.execute(
        """INSERT INTO perp_liquidity_5m (venue,asset,bucket_start,complete,book_samples,
           spread_bps_mean,spread_bps_max,mid_last,bid_depth_10bps_mean,ask_depth_10bps_mean,
           bid_depth_25bps_mean,ask_depth_25bps_mean,depth_25bps_covered_fraction,
           buy_trades,sell_trades,buy_volume,sell_volume,first_event_at,last_event_at,
           recorded_at,long_liq_volume,short_liq_volume,long_liq_count,short_liq_count)
           VALUES (?, 'BTC', ?, 1, 10, ?, 2.0, 100, ?, ?, ?, ?, 1, 5, 5, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
        (venue, start.isoformat(), spread, bid, ask, bid, ask, buy, sell,
         start.isoformat(), start.isoformat(), recorded.isoformat(),
         long_liq, short_liq))


def _context(conn, observed, funding=0.0, oi=1000.0, mark=100.0, oracle=100.0):
    conn.execute(
        """INSERT INTO perp_market_contexts
           (venue,contract,quote,observed_at,mark_price,oracle_price,
            funding_rate,open_interest_base)
           VALUES ('hyperliquid','BTC','USDC',?,?,?,?,?)""",
        (observed.isoformat(), mark, oracle, funding, oi))


class _Bundle:
    """Fake live BinanceContextFeed bundle (in-memory, like the real one)."""

    def __init__(self, funding=(), oi=()):
        self._funding = funding
        self._oi = oi

    def bundle(self, asset):
        return {"funding": self._funding, "open_interest": self._oi}


def _inputs(challenger, now=NOW, assets=("BTC",)):
    return challenger.asset_inputs(now, set(assets))


def test_inputs_never_use_buckets_recorded_after_issuance(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            _bucket(conn, "hyperliquid", NOW - timedelta(minutes=10),
                    NOW - timedelta(minutes=5), buy=9, sell=1)
            # Recorded AFTER the forecast clock: must be invisible.
            _bucket(conn, "hyperliquid", NOW - timedelta(minutes=5),
                    NOW + timedelta(seconds=10), buy=0, sell=10)
        flow = _inputs(FlowChallengerV3(store))["BTC"]
        assert len(flow) == N_FLOW
        assert flow[8] == (9 - 1) / 10  # hl_tape_imb_5m: only the bucket known at issuance
        assert flow[-3] == 1.0  # hl_liquidity flag
        assert flow[-2] == 0.0 and flow[-1] == 0.0  # no OKX / Coinbase buckets
        # No data at all: the asset is skipped, not issued an all-zero vector.
        assert _inputs(FlowChallengerV3(store), assets=("ETH",)) == {}
    finally:
        store.close()


def test_binance_bundle_hyperliquid_context_and_liquidations(tmp_path):
    store = _store(tmp_path)
    try:
        bundle = _Bundle(
            funding=[(NOW - timedelta(minutes=10), 0.0001)],
            oi=[(NOW - timedelta(minutes=60), 100.0),
                (NOW - timedelta(minutes=5), 110.0)],
        )
        with store.connect() as conn:
            start = NOW - timedelta(minutes=10)
            _bucket(conn, "hyperliquid", start, NOW - timedelta(minutes=4), buy=9, sell=1)
            _bucket(conn, "okx", start, NOW - timedelta(minutes=4), buy=3, sell=1,
                    bid=2.0, ask=2.0, long_liq=30.0, short_liq=10.0)
            _bucket(conn, "coinbase", start, NOW - timedelta(minutes=4), buy=1, sell=3,
                    bid=2.0, ask=2.0)
            _context(conn, NOW - timedelta(minutes=60), funding=0.0002, oi=1000.0)
            _context(conn, NOW - timedelta(minutes=2), funding=0.0002, oi=1100.0)
        flow = _inputs(FlowChallengerV3(store, binance_context=bundle))["BTC"]
        (bin_fund, hl_fund, bin_oi, hl_oi, hl_d10, hl_d25, okx_d10, okx_d25,
         hl_t5, hl_t15, okx_t5, okx_t15, cb_t5, cb_t15, liq_net, liq_share,
         hl_spread, *flags) = flow
        assert bin_fund == 1.0  # 0.0001 * 1e4 bps
        assert hl_fund == 2.0  # 0.0002 * 1e4 bps
        assert abs(bin_oi - 0.1) < 1e-9  # 110/100 - 1 over the hour
        assert hl_oi == math.log(1.1)  # log OI ratio vs context >=55 min old
        assert hl_d10 == hl_d25 == (10 - 5) / 15
        assert okx_d10 == okx_d25 == 0.0  # balanced 2 vs 2 books
        assert hl_t5 == hl_t15 == 0.8 and okx_t5 == okx_t15 == 0.5
        assert cb_t5 == cb_t15 == -0.5
        assert liq_net == 0.5  # (30-10)/40, OKX-only
        assert liq_share == 10.0  # 40 liq volume / 4 trade volume
        assert hl_spread == 1.0
        assert flags == [1.0] * 5  # every source available
    finally:
        store.close()


def test_stale_hyperliquid_context_and_missing_bundle_flag_zero(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            _bucket(conn, "hyperliquid", NOW - timedelta(minutes=10),
                    NOW - timedelta(minutes=4), buy=9, sell=1)
            # Context 6 minutes old: older than one 5-minute collect cycle.
            _context(conn, NOW - timedelta(minutes=6), funding=0.0002, oi=1000.0)
        flow = _inputs(FlowChallengerV3(store))["BTC"]
        assert flow[1] == 0.0  # hl funding stale -> 0.0
        assert flow[3] == 0.0  # hl OI: no context >=55 min old either
        assert flow[-4] == 0.0  # hl_context flag
        assert flow[0] == 0.0 and flow[-5] == 0.0  # no Binance bundle at all
    finally:
        store.close()


def test_call_refuses_all_zero_flags_and_persists_exact_inputs(tmp_path):
    store = _store(tmp_path)
    challenger = FlowChallengerV3(store)
    try:
        flow = (0.5,) + (0.0,) * 16 + (1.0, 0.0, 0.0, 0.0, 0.0)
        zero_flags = (0.0,) * N_FLOW
        with store.connect() as conn:
            assert challenger.call(
                conn, venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
                asset="BTC", horizon=5, reference_opened_at=NOW, issued_at=NOW,
                features=(0.1,) * 10, flow=zero_flags) is None
            call = challenger.call(
                conn, venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
                asset="BTC", horizon=5, reference_opened_at=NOW, issued_at=NOW,
                features=(0.1,) * 10, flow=flow)
        assert call["family"] == FAMILY and 0 < call["probability"] < 1
        assert len(call["model_sha256"]) == 64
        with store.connect() as conn:
            row = conn.execute("SELECT * FROM rival_flow3_features").fetchone()
        inputs = json.loads(row["features"])
        raw = json.loads(row["raw_features"])
        assert len(inputs) == 32 and len(raw) == 27  # 10 price + 17 flow + 5 flags
        assert raw[:10] == [0.1] * 10 and raw[10] == 0.5
        assert inputs[-5:] == [1.0, 0.0, 0.0, 0.0, 0.0]  # flags pass through unstandardized
    finally:
        store.close()


def test_learns_only_from_graded_outcomes_and_skips_no_trade_targets(tmp_path):
    store = _store(tmp_path)
    challenger = FlowChallengerV3(store)
    try:
        flow = (0.5,) + (0.0,) * 16 + (1.0, 0.0, 0.0, 0.0, 0.0)
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
            state = conn.execute("SELECT * FROM rival_flow3_model_state").fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_flow3_features WHERE learned=0").fetchone()["n"]
        assert state["updates"] == 1 and state["positives"] == 1
        assert state["graded_count"] == 1
        assert state["feature_digest"] == FEATURE_DIGEST
        assert state["trained_through"] == NOW.isoformat()
        assert len(json.loads(state["weights"])) == 33
        assert pending == 1  # the unsettled forecast waits for its real outcome
        assert challenger.learn(NOW) == 0  # nothing is learned twice
    finally:
        store.close()


def test_standardization_uses_only_past_graded_data(tmp_path):
    store = _store(tmp_path)
    challenger = FlowChallengerV3(store)
    try:
        flow = (0.0,) * N_FLOW
        flow = (0.0,) * 17 + (1.0, 0.0, 0.0, 0.0, 0.0)  # one flag so call() accepts it
        features = (4.0,) + (0.0,) * 9  # price feature 0 is 4.0 every time
        with store.connect() as conn:
            for i in range(2):
                ref = NOW - timedelta(minutes=30 - 5 * i)
                challenger.call(conn, venue="okx", contract="BTC-USDT-SWAP",
                                quote="USDT", asset="BTC", horizon=5,
                                reference_opened_at=ref, issued_at=ref,
                                features=features, flow=flow)
                conn.execute("INSERT INTO perp_forward_audit VALUES "
                             "('okx','BTC-USDT-SWAP','USDT',5,?,?,1)",
                             (ref.isoformat(), NOW.isoformat()))
            first = json.loads(conn.execute(
                "SELECT features FROM rival_flow3_features ORDER BY issued_at"
                ).fetchone()["features"])
        assert first[0] == 4.0  # no graded data yet: standardization is identity
        assert challenger.learn(NOW) == 2
        with store.connect() as conn:
            stats = json.loads(conn.execute(
                "SELECT stats_json FROM rival_flow3_model_state").fetchone()["stats_json"])
            # Issuing without learning must not move the trailing stats.
            challenger.call(conn, venue="okx", contract="BTC-USDT-SWAP",
                            quote="USDT", asset="BTC", horizon=5,
                            reference_opened_at=NOW, issued_at=NOW,
                            features=features, flow=flow)
            after = json.loads(conn.execute(
                "SELECT features FROM rival_flow3_features "
                "ORDER BY issued_at DESC LIMIT 1").fetchone()["features"])
            stats_after = json.loads(conn.execute(
                "SELECT stats_json FROM rival_flow3_model_state").fetchone()["stats_json"])
        assert stats["count"] == 2 and stats["mean"][0] == 4.0
        assert stats_after["count"] == 2  # the ungraded issue changed nothing
        assert after[0] == 0.0  # (4.0 - trailing mean 4.0): past graded data only
    finally:
        store.close()


def test_young_model_stays_near_the_up_rate():
    strong = {"weights": [0.0] + [3.0] * 32, "updates": 10, "positives": 5,
              "stats": {"count": 0, "mean": [0.0] * 27, "m2": [0.0] * 27},
              "calibration": {"status": "unevaluated"}}
    inputs = (1.0,) * 32
    young = FlowChallengerV3.probability(strong, inputs)
    assert abs(young - 0.5) < 0.05  # 10 graded results earn almost no say
    seasoned = dict(strong, updates=3000, positives=1500)
    assert FlowChallengerV3.probability(seasoned, inputs) > 0.85


def test_platt_gate_catches_overconfident_calls(tmp_path):
    # Fit on roughly calibrated rows, then gate on the Flow failure mode:
    # ~78%-UP calls true only 40% of the time.
    fit_rows = ([(0.8, 1)] * 16 + [(0.8, 0)] * 4
                + [(0.2, 0)] * 16 + [(0.2, 1)] * 4)
    overconfident = [(0.78, 1)] * 12 + [(0.78, 0)] * 18
    result = fit_platt(fit_rows, overconfident)
    assert result["status"] == "failed_gate"
    assert result["ece"] is not None and result["ece"] > 0.10
    assert result["top_bucket_hit_rate"] == 0.4  # 12/30, not ~0.78
    # Well-calibrated evaluation passes the same gate.
    good = fit_platt(fit_rows, [(0.8, 1)] * 24 + [(0.8, 0)] * 6)
    assert good["status"] == "evaluated" and good["ece"] <= 0.10
    # Too little data: unevaluated, never a claimed calibration.
    assert fit_platt([(0.8, 1)] * 5, [(0.8, 1)] * 5)["status"] == "unevaluated"


def test_probability_serves_raw_until_calibration_passes():
    state = {"weights": [0.0] * 33, "updates": 100, "positives": 50,
             "calibration": {"status": "unevaluated"}}
    inputs = (0.0,) * 32
    assert FlowChallengerV3.probability(state, inputs) == 0.5
    calibrated = dict(state, calibration={"status": "evaluated", "a": 2.0, "b": 0.0})
    strong_inputs = (1.0,) * 32
    # Raw is shrunk toward 0.5; a passing Platt fit sharpens it.
    raw = FlowChallengerV3.raw_probability(
        dict(calibrated, weights=[0.0] + [1.0] * 32), strong_inputs)
    served = FlowChallengerV3.probability(
        dict(calibrated, weights=[0.0] + [1.0] * 32), strong_inputs)
    assert served > raw > 0.5
    gate = calibration_gate(
        dict(calibrated, calibration={"status": "evaluated", "a": 1.0, "b": 0.0}),
        [(0.8, 1)] * 24 + [(0.8, 0)] * 6)
    assert gate["passes"] is True


def test_learn_refits_calibration_from_graded_history_only(tmp_path):
    store = _store(tmp_path)
    challenger = FlowChallengerV3(store)
    try:
        flow = (0.0,) * 17 + (1.0, 0.0, 0.0, 0.0, 0.0)
        with store.connect() as conn:
            for i in range(100):
                ref = NOW - timedelta(minutes=500 - 5 * i)
                challenger.call(conn, venue="okx", contract="BTC-USDT-SWAP",
                                quote="USDT", asset="BTC", horizon=5,
                                reference_opened_at=ref, issued_at=ref,
                                features=(0.0,) * 10, flow=flow)
                conn.execute("INSERT INTO perp_forward_audit VALUES "
                             "('okx','BTC-USDT-SWAP','USDT',5,?,?,?)",
                             (ref.isoformat(), NOW.isoformat(), i % 2))
        assert challenger.learn(NOW) == 100
        status = FlowChallengerV3.calibration_status(store, "BTC", 5)
        assert status["method"] == "platt"
        # Flat 0.5 scores, balanced labels: ECE ~0, no confident bucket.
        assert status["status"] == "evaluated" and abs(status["ece"]) < 1e-3
        assert status["liquidation_coverage"] == "OKX-only"
    finally:
        store.close()


def _seed_cycle_candles(market, *, now, bars=150):
    """150 live 5m bars on hyperliquid/BTC, as the cycle diagnostics use."""
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    market.initialize()
    with market.store.connect() as conn:
        for i in range(bars):
            opened = boundary - timedelta(minutes=5 * (bars - i))
            closed = opened + timedelta(minutes=5, milliseconds=-1)
            observed = closed + timedelta(milliseconds=1)
            p = 100 + i / 15 + (i % 9) / 8
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "hyperliquid", "BTC", "USDC", 5,
                    opened.isoformat(), closed.isoformat(), observed.isoformat(),
                    p, p + 2, p - 2, p + 0.5, 10, 12,
                ),
            )


def test_retirement_flag_stops_flow_v3_issuance_in_cycle(tmp_path, monkeypatch):
    """Retired Nov 7, 2026: the cycle issues no new flow_v3 forecasts.

    Control first: with the flag forced back on, the same cycle issues
    flow_v3 forecasts (features rows + shadow rows). With the flag off —
    its production value — the same cycle issues nothing new for flow_v3,
    while the incumbent cycle still runs.
    """
    assert forward_audit.ISSUE_FLOW_V3_CHALLENGERS is False
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "retire.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        FlowChallengerV3(store).initialize()
        _seed_cycle_candles(market, now=now)
        with store.connect() as conn:
            _bucket(conn, "hyperliquid", now - timedelta(minutes=10),
                    now - timedelta(minutes=4), buy=9, sell=1)
            _context(conn, now - timedelta(minutes=2), funding=0.0002, oi=1100.0)
            _context(conn, now - timedelta(minutes=60), funding=0.0002, oi=1000.0)

        def _flow3_counts():
            with store.connect() as conn:
                features = conn.execute(
                    "SELECT COUNT(*) AS n FROM rival_flow3_features"
                ).fetchone()["n"]
                shadow = conn.execute(
                    "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
                    "WHERE family='flow_v3'"
                ).fetchone()["n"]
            return features, shadow

        monkeypatch.setattr(forward_audit, "ISSUE_FLOW_V3_CHALLENGERS", True)
        monitor.run_once(now=now)
        features_on, shadow_on = _flow3_counts()
        assert features_on >= 1  # the control proves this cycle can issue flow_v3
        assert shadow_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_FLOW_V3_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _flow3_counts() == (features_on, shadow_on)
    finally:
        store.close()


def test_retired_flow_v3_rows_still_settle(tmp_path):
    """Already-issued flow_v3 rows keep settling after retirement.

    Settlement is flag-independent: ForwardAccuracyMonitor._settle grades
    every pending perp_forward_audit row, and ShadowModelCompetition.settle
    grades every shadow row from its verified incumbent outcome. Neither
    path reads ISSUE_FLOW_V3_CHALLENGERS.
    """
    start = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    now = start + timedelta(hours=2)
    target = start + timedelta(minutes=60)
    ref = target - timedelta(minutes=5)
    issued = target - timedelta(seconds=40)
    store = SQLiteHotStore(tmp_path / "retire-settle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        market.initialize()
        monitor.initialize()
        challenger = FlowChallengerV3(store)
        challenger.initialize()
        flow = (0.5,) + (0.0,) * 16 + (1.0, 0.0, 0.0, 0.0, 0.0)
        with store.connect() as conn:
            call = challenger.call(
                conn, venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5, reference_opened_at=ref, issued_at=issued,
                features=(0.1,) * 10, flow=flow)
            assert call is not None and call["family"] == FAMILY
            # The incumbent audit row this forecast is graded against.
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
                 ref.isoformat(), 100.0, issued.isoformat(), target.isoformat(),
                 0.7, 0.5, 50))
            # Target candle closes UP from the reference.
            closed = target + timedelta(minutes=5) - timedelta(milliseconds=1)
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", 5, target.isoformat(),
                 closed.isoformat(), (target + timedelta(minutes=6)).isoformat(),
                 101.0, 102.0, 100.0, 101.0, 10, 20))
            # The shadow-ledger row for the same forecast.
            ShadowModelCompetition(store).initialize()
            conn.execute(
                """INSERT INTO rival_model_shadow_forecasts
                   (venue,contract,quote,asset,horizon_minutes,family,
                    reference_opened_at,issued_at,target_opened_at,
                    probability_up,incumbent_up,baseline_up,
                    training_count,training_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, "flow_v3",
                 ref.isoformat(), issued.isoformat(), target.isoformat(),
                 0.7, 0.7, 0.5, 1, "a" * 64))
        assert monitor._settle(now) >= 1
        with store.connect() as conn:
            row = conn.execute(
                "SELECT settled_at, outcome_up FROM perp_forward_audit"
            ).fetchone()
        assert row["settled_at"] is not None and row["outcome_up"] == 1
        assert ShadowModelCompetition(store).settle(now=now) >= 1
        with store.connect() as conn:
            shadow = conn.execute(
                "SELECT settled_at, outcome_up FROM rival_model_shadow_forecasts "
                "WHERE family='flow_v3'"
            ).fetchone()
        assert shadow["settled_at"] is not None and shadow["outcome_up"] == 1
    finally:
        store.close()


def _promotion_row(store, *, family, evaluated_at, artifact, asset="BTC"):
    FamilyPromotion(store).initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                selected_family,status,reason,evidence_json)
               VALUES ('hyperliquid','BTC','USDC',?,5,?,?,'promoted_paper','ok',?)
               ON CONFLICT(venue,contract,quote,horizon_minutes)
               DO UPDATE SET evaluated_at=excluded.evaluated_at,
                selected_family=excluded.selected_family,
                status=excluded.status, reason=excluded.reason,
                evidence_json=excluded.evidence_json""",
            (asset, evaluated_at.isoformat(), family,
             json.dumps({"flow_v3": {"artifact": artifact}})))


def _seed_flow3_state(store):
    """One graded flow_v3 row, learned: returns the current artifact."""
    challenger = FlowChallengerV3(store)
    flow = (0.0,) * 17 + (1.0, 0.0, 0.0, 0.0, 0.0)
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_forward_audit (
            venue TEXT, contract TEXT, quote TEXT, horizon_minutes INTEGER,
            reference_opened_at TEXT, settled_at TEXT, outcome_up INTEGER)""")
        ref = NOW - timedelta(minutes=30)
        challenger.call(conn, venue="hyperliquid", contract="BTC", quote="USDC",
                        asset="BTC", horizon=5, reference_opened_at=ref,
                        issued_at=ref, features=(0.1,) * 10, flow=flow)
        conn.execute("INSERT INTO perp_forward_audit VALUES "
                     "('hyperliquid','BTC','USDC',5,?,?,1)",
                     (ref.isoformat(), NOW.isoformat()))
    assert challenger.learn(NOW) == 1
    return FlowChallengerV3.artifact_identity(store, "BTC", 5)


def test_selected_identity_check_and_blocked_families(tmp_path):
    store = _store(tmp_path)
    try:
        artifact = _seed_flow3_state(store)
        assert artifact is not None and artifact["feature_digest"] == FEATURE_DIGEST
        _promotion_row(store, family="flow_v3", evaluated_at=NOW,
                       artifact=artifact)
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                      "quote": "USDC", "horizon": 5, "now": NOW}
        assert promoter.selected(**kwargs) == "flow_v3"
        # Kill switch 2: blocked even with a fresh promoted_paper row.
        BLOCKED_FAMILIES.add("flow_v3")
        try:
            assert promoter.selected(**kwargs) is None
        finally:
            BLOCKED_FAMILIES.discard("flow_v3")
        assert promoter.selected(**kwargs) == "flow_v3"
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="flow_v3",
                       evaluated_at=NOW - timedelta(hours=49), artifact=artifact)
        assert promoter.selected(**kwargs) is None
        _promotion_row(store, family="flow_v3", evaluated_at=NOW,
                       artifact=artifact)
        # A mismatched weights SHA with no further graded learning: not the
        # artifact that earned promotion.
        tampered = dict(artifact, weights_sha256="0" * 64)
        _promotion_row(store, family="flow_v3", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # A refit/reset (state replaced, training cursor gone) never serves
        # on the name alone.
        _promotion_row(store, family="flow_v3", evaluated_at=NOW,
                       artifact=artifact)
        with store.connect() as conn:
            conn.execute(
                "UPDATE rival_flow3_model_state SET weights=?, updates=0, "
                "positives=0, graded_count=0, trained_through=NULL, "
                "weights_sha256=? WHERE asset='BTC' AND horizon_minutes=5",
                (json.dumps([0.0] * 33), "1" * 64))
        assert promoter.selected(**kwargs) is None
    finally:
        store.close()


def test_blocked_families_blocks_builtin_families_too(tmp_path):
    store = _store(tmp_path)
    try:
        _promotion_row(store, family="gaussian_nb_v1", evaluated_at=NOW,
                       artifact=None)
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                      "quote": "USDC", "horizon": 5, "now": NOW}
        assert promoter.selected(**kwargs) == "gaussian_nb_v1"
        BLOCKED_FAMILIES.add("gaussian_nb_v1")
        try:
            assert promoter.selected(**kwargs) is None
        finally:
            BLOCKED_FAMILIES.discard("gaussian_nb_v1")
    finally:
        store.close()


def test_promotion_receipt_dedupe_and_status_card_delivery(tmp_path):
    store = SQLiteHotStore(tmp_path / "receipts.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS perp_forward_audit "
                         "(asset TEXT, horizon_minutes INTEGER, issued_at TEXT)")
            conn.execute("INSERT INTO perp_forward_audit VALUES ('BTC', 5, ?)",
                         ((NOW - timedelta(minutes=3)).isoformat(),))
            conn.execute("""CREATE TABLE rival_pooled_models (trained_through TEXT,
                            gates_passed INTEGER)""")
            conn.execute("INSERT INTO rival_pooled_models VALUES (?,1)",
                         (str(NOW - timedelta(days=2)).replace("T", " "),))
            conn.execute("CREATE TABLE rival_intelligence_calls (issued_at TEXT)")
            conn.execute("INSERT INTO rival_intelligence_calls VALUES (?)",
                         ((NOW - timedelta(minutes=10)).isoformat(),))
            conn.execute("""CREATE TABLE IF NOT EXISTS rival_model_shadow_forecasts (
                            family TEXT, horizon_minutes INTEGER, settled_at TEXT,
                            model_brier REAL, baseline_brier REAL, venue TEXT,
                            training_sha256 TEXT, issued_at TEXT, target_opened_at TEXT)""")
        evidence = {"targets": 180, "days": 22, "challenger_trades": 60,
                    "quote_coverage": 0.9, "ece": 0.05,
                    "cost_proof": {
                        "conservative": {"paired_gain_bps": 3.0},
                        "severe": {"paired_gain_bps": 1.5}}}
        calibration = {"status": "evaluated", "ece": 0.05,
                       "top_bucket_hit_rate": 0.8, "top_bucket_n": 40}
        receipt = record_promotion_receipt(
            store, family="flow_v3", venue="hyperliquid", contract="BTC",
            quote="USDC", horizon=5, kind="promoted", evidence=evidence,
            calibration=calibration, now=NOW)
        assert receipt is not None
        assert "targets=180" in receipt["receipt_text"]
        assert "severe_paired_gain_bps=1.5" in receipt["receipt_text"]
        assert "top_bucket_hit_rate=0.8" in receipt["receipt_text"]
        assert "OKX-only coverage" in receipt["receipt_text"]
        # Second recording of the same first event: no duplicate receipt.
        assert record_promotion_receipt(
            store, family="flow_v3", venue="hyperliquid", contract="BTC",
            quote="USDC", horizon=5, kind="promoted", evidence=evidence,
            calibration=calibration, now=NOW) is None
        keys = {key for key, _ in research_alerts(store, now=NOW)}
        assert "flow-v3:promoted:hyperliquid:BTC:5m" in keys
    finally:
        store.close()
