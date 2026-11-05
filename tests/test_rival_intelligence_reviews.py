"""RivaL Intelligence reviews model calls; it never invents a direction."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.intelligence import (
    IntelligenceLedger,
    consensus_direction,
    model_calls,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

VENUES = ("okx", "gate", "kucoin")


def _forecast(conn, *, venue, issued, target, p, outcome=None):
    conn.execute(
        """INSERT INTO perp_forward_audit (
             venue,contract,quote,asset,horizon_minutes,model_version,
             reference_opened_at,reference_close,issued_at,target_opened_at,
             probability_up,baseline_up,training_evaluated,settled_at,outcome_up,
             realized_close,model_brier,baseline_brier
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (venue, "BTC", "USDT", "BTC", 5, MODEL, (target - timedelta(minutes=5)).isoformat(),
         100., issued.isoformat(), target.isoformat(), p, .5, 100,
         (target + timedelta(minutes=6)).isoformat() if outcome is not None else None,
         outcome, 101. if outcome is not None else None,
         (p - outcome) ** 2 if outcome is not None else None,
         (.5 - outcome) ** 2 if outcome is not None else None),
    )


def test_consensus_requires_clear_agreement():
    assert consensus_direction({"p_up": .56, "venues": 9, "venues_up": 8}) == "up"
    assert consensus_direction({"p_up": .44, "venues": 9, "venues_up": 1}) == "down"
    assert consensus_direction({"p_up": .51, "venues": 9, "venues_up": 9}) is None
    assert consensus_direction({"p_up": .56, "venues": 9, "venues_up": 5}) is None
    assert consensus_direction(None) is None


def test_reviews_are_recorded_against_model_slot_and_graded(tmp_path):
    store = SQLiteHotStore(tmp_path / "intel.sqlite")
    market = PerpetualMarketStore(store)
    try:
        ForwardAccuracyMonitor(market).initialize()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        target = now + timedelta(minutes=1)
        with store.connect() as conn:
            for venue in VENUES:
                _forecast(conn, venue=venue, issued=now - timedelta(seconds=30),
                          target=target, p=.6)
        calls = model_calls(market, asset="BTC", now=now, summary={})
        live = calls["5"]["live"]
        assert (live["venues"], live["venues_up"]) == (3, 3)
        ledger = IntelligenceLedger(store)
        review = {"stance_5m": "follow", "stance_15m": "abstain", "stance_60m": "abstain",
                  "thesis": "All three exchanges lean up.", "risk": "Small edge."}
        assert ledger.record(asset="BTC", calls=calls, review=review, model="lfm3",
                             issued_at=now) == 1
        # Too late: the slot's result window had already opened.
        late = target + timedelta(minutes=5)
        assert ledger.record(asset="BTC", calls=calls, review=review, model="lfm3",
                             issued_at=late) == 0
        with store.connect() as conn:
            conn.execute("UPDATE perp_forward_audit SET settled_at=?, outcome_up=1, "
                         "model_brier=.16, baseline_brier=.25",
                         ((target + timedelta(minutes=6)).isoformat(),))
        graded = ledger.report(asset="BTC", horizon=5, now=now + timedelta(minutes=10))
        assert graded["followed"] == 1 and graded["followed_hit_rate"] == 1.0
        assert graded["abstained"] == 0
        assert ledger.latest(asset="BTC")["stances"][5] == ("follow", "up")
    finally:
        store.close()


def test_follow_without_clear_consensus_makes_no_call(tmp_path):
    store = SQLiteHotStore(tmp_path / "intel2.sqlite")
    market = PerpetualMarketStore(store)
    try:
        ForwardAccuracyMonitor(market).initialize()
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        target = now + timedelta(minutes=1)
        with store.connect() as conn:
            for venue, p in zip(VENUES, (.6, .4, .55)):
                _forecast(conn, venue=venue, issued=now - timedelta(seconds=30),
                          target=target, p=p)
        calls = model_calls(market, asset="BTC", now=now, summary={})
        ledger = IntelligenceLedger(store)
        ledger.record(asset="BTC", calls=calls, model="lfm3", issued_at=now, review={
            "stance_5m": "follow", "stance_15m": "follow", "stance_60m": "follow",
            "thesis": "Mixed.", "risk": "Mixed."})
        assert ledger.latest(asset="BTC")["stances"][5] == ("follow", "none")
    finally:
        store.close()


def test_market_context_comes_from_rivals_own_feeds_and_never_the_future(tmp_path):
    from parlay_bot.perpetuals.intelligence import market_context
    from parlay_bot.perpetuals.liquidity_record import SCHEMA as LIQUIDITY

    store = SQLiteHotStore(tmp_path / "ctx.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        now = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
        with store.connect() as conn:
            conn.executescript(LIQUIDITY)
            for at, mark, oi in ((now - timedelta(minutes=60), 100.0, 1000.0),
                                 (now - timedelta(minutes=2), 100.1, 1100.0),
                                 (now + timedelta(minutes=1), 150.0, 9000.0)):  # future
                conn.execute("INSERT INTO perp_market_contexts VALUES "
                             "('hyperliquid','BTC','USDC',?,?,100.0,0.0000125,?)",
                             (at.isoformat(), mark, oi))
            for start, rec, buy, sell in ((now - timedelta(minutes=30), now - timedelta(minutes=25), 6, 4),
                                          (now - timedelta(minutes=10), now + timedelta(minutes=1), 0, 99)):
                conn.execute(
                    """INSERT INTO perp_liquidity_5m (venue,asset,bucket_start,complete,
                       book_samples,spread_bps_mean,spread_bps_max,mid_last,buy_trades,
                       sell_trades,buy_volume,sell_volume,first_event_at,last_event_at,
                       recorded_at) VALUES ('okx','BTC',?,1,5,1,2,100,1,1,?,?,?,?,?)""",
                    (start.isoformat(), buy, sell, start.isoformat(), start.isoformat(),
                     rec.isoformat()))
        ctx = market_context(market, asset="BTC", now=now)
        assert ctx["premium_bps"] == 10.0
        assert ctx["funding_bps"] == 1.0  # hourly 0.00125% as an 8-hour rate
        assert ctx["open_interest_change_1h"] == 0.1
        assert ctx["taker_buy_share_1h"] == 0.6  # the future bucket is invisible
        assert market_context(market, asset="ETH", now=now) == {}  # missing, not guessed
    finally:
        store.close()


def test_reviewer_sees_each_challengers_real_record_best_three_only(tmp_path):
    from parlay_bot.perpetuals import intelligence
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition

    intelligence._RECORDS.clear()
    store = SQLiteHotStore(tmp_path / "rec.sqlite")
    market = PerpetualMarketStore(store)
    try:
        ForwardAccuracyMonitor(market).initialize()
        ShadowModelCompetition(store).initialize()
        now = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
        target = now + timedelta(minutes=1)
        families = {"calib_incumbent_v1": .70, "flow_v1": .55, "flow_v2": .52,
                    "pooled_5m_aaaa": .45, "knn21_v1": .9}  # knn is not a reviewed family
        with store.connect() as conn:
            for family, p in families.items():
                for i in range(60):  # graded history: outcome always UP
                    ref = (now - timedelta(days=2, minutes=5 * i)).isoformat()
                    conn.execute(
                        """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,
                           asset,horizon_minutes,family,reference_opened_at,issued_at,
                           target_opened_at,probability_up,incumbent_up,baseline_up,
                           training_count,training_sha256,settled_at,outcome_up,
                           model_brier,incumbent_brier,baseline_brier)
                           VALUES ('okx','BTC','USDT','BTC',5,?,?,?,?,?,.5,.5,1,'x',?,1,?,.25,.25)""",
                        (family, ref, ref, ref, p, (now - timedelta(days=1)).isoformat(),
                         (p - 1) ** 2))
                conn.execute(  # the current slot's call
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                       horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                       probability_up,incumbent_up,baseline_up,training_count,training_sha256)
                       VALUES ('okx','BTC','USDT','BTC',5,?,?,?,?,?,.5,.5,1,'x')""",
                    (family, now.isoformat(), (now - timedelta(seconds=30)).isoformat(),
                     target.isoformat(), p))
        calls = model_calls(market, asset="BTC", now=now, summary={})["5"]
        assert set(calls) == {"calib_incumbent_v1", "flow_v1", "flow_v2"}  # best three
        record = calls["calib_incumbent_v1"]["record"]
        assert record == {"hit_rate": 1.0, "graded": 60, "beats_basic": True}
        assert "knn21_v1" not in calls and "pooled_5m_aaaa" not in calls
    finally:
        store.close()


def test_a_few_lucky_results_never_outrank_a_long_record():
    from parlay_bot.perpetuals.intelligence import MIN_GRADED_TO_RANK, _rank_key

    lucky = {"graded": 3, "brier_gain": 0.2}
    solid = {"graded": 500, "brier_gain": 0.004}
    even = {"graded": 500, "brier_gain": 0.0}
    none = {}
    order = sorted([("lucky", lucky), ("none", none), ("even", even), ("solid", solid)],
                   key=lambda item: _rank_key(item[1]))
    assert [name for name, _ in order] == ["solid", "even", "lucky", "none"]
    # Exactly at the bar, a record is ranked on its gain.
    assert _rank_key({"graded": MIN_GRADED_TO_RANK, "brier_gain": 0.1})[0] == 0
