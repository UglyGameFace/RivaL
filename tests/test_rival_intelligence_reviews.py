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
