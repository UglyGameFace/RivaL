"""Bot Performance numbers must be internally consistent and honestly scoped."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.home import score_consistency_problems
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def _insert(conn, *, venue, issued, settled_at=None, probability=.7, result=None,
            model=MODEL):
    target = issued + timedelta(minutes=5)
    conn.execute(
        """INSERT INTO perp_forward_audit (
             venue,contract,quote,asset,horizon_minutes,model_version,
             reference_opened_at,reference_close,issued_at,
             target_opened_at,probability_up,baseline_up,
             training_evaluated,settled_at,outcome_up,realized_close,
             model_brier,baseline_brier
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            venue, "BTC", "USDC", "BTC", 5, model,
            issued.isoformat(), 100., issued.isoformat(), target.isoformat(),
            probability, .5, 100,
            settled_at.isoformat() if settled_at else None,
            result, 101. if result is not None else None,
            (probability - result) ** 2 if result is not None else None,
            (.5 - result) ** 2 if result is not None else None,
        ),
    )


def test_summary_separates_not_due_waiting_and_overdue(tmp_path):
    store = SQLiteHotStore(tmp_path / "due.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        monitor.initialize()
        now = datetime.now(UTC).replace(microsecond=0)
        with store.connect() as conn:
            # Two exchanges forecasting the same settled window.
            for venue in ("hyperliquid", "dydx"):
                _insert(conn, venue=venue, issued=now - timedelta(minutes=30),
                        settled_at=now - timedelta(minutes=20), result=1)
            # Target candle closed 3 hours ago, never settled: overdue.
            _insert(conn, venue="hyperliquid", issued=now - timedelta(hours=3),)
            # Target closed 10 min ago, not settled yet: due but not overdue.
            _insert(conn, venue="dydx", issued=now - timedelta(minutes=20))
            # Target candle still open: not due.
            _insert(conn, venue="hyperliquid", issued=now - timedelta(minutes=2))
            # A retired model version must never be counted.
            _insert(conn, venue="hyperliquid", issued=now - timedelta(minutes=40),
                    settled_at=now - timedelta(minutes=30), result=0,
                    model="retired-v1")
        score = monitor.summary(asset="BTC", now=now)["last_7d"][5]
        assert score["issued"] == 5
        assert score["due"] == 4
        assert score["overdue"] == 1
        assert score["settled"] == 2
        assert score["windows"] == 1
        assert score["venues"] == 2
        assert score_consistency_problems(score) == []
    finally:
        store.close()


def test_venue_summary_defaults_to_live_model_only(tmp_path):
    store = SQLiteHotStore(tmp_path / "venue.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        monitor.initialize()
        now = datetime.now(UTC).replace(microsecond=0)
        with store.connect() as conn:
            _insert(conn, venue="hyperliquid", issued=now - timedelta(minutes=30),
                    settled_at=now - timedelta(minutes=20), result=1)
            _insert(conn, venue="hyperliquid", issued=now - timedelta(minutes=40),
                    settled_at=now - timedelta(minutes=30), result=0,
                    model="retired-v1")
        report = monitor.venue_summary(asset="BTC", venue="hyperliquid", horizon=5,
                                       now=now)
        assert report["model_version"] == MODEL
        assert report["issued"] == 1
        assert report["settled"] == 1
    finally:
        store.close()


def test_consistent_counts_pass():
    assert score_consistency_problems({
        "issued": 10, "due": 8, "overdue": 1, "settled": 7, "scored": 7,
        "decided": 6, "correct": 4, "hit_rate": 4 / 6, "brier": .24,
        "baseline_brier": .25,
    }) == []


def test_impossible_counts_are_reported():
    problems = score_consistency_problems({
        "issued": 5, "due": 6, "overdue": 3, "settled": 7, "scored": 8,
        "decided": 9, "correct": 10, "hit_rate": 1.4, "brier": 1.2,
    })
    text = "; ".join(problems)
    for expected in ("settled 7 > issued 5", "scored 8 > settled 7",
                     "decided 9 > scored 8", "correct 10 > decided 9",
                     "due 6 > issued 5", "settled 7 > due 6",
                     "hit rate 1.4 outside 0-1", "brier 1.2 outside 0-1"):
        assert expected in text


def test_hit_rate_must_match_its_counts():
    problems = score_consistency_problems({
        "issued": 10, "settled": 10, "decided": 10, "correct": 5, "hit_rate": .9,
    })
    assert any("hit rate" in p for p in problems)


def test_improvement_result_is_reused_after_a_slow_first_run(monkeypatch):
    from types import SimpleNamespace

    from parlay_bot.discord_app import home as home_module

    calls = []

    def slow(_market, asset, horizon):
        calls.append((asset, horizon))
        return {"status": "insufficient", "targets": 957, "days": 4}

    monkeypatch.setattr(home_module, "_performance_benefit", slow)
    market = SimpleNamespace()
    first = home_module._cached_benefit(market, "BTC", 5)
    second = home_module._cached_benefit(market, "BTC", 5)
    assert first == second
    assert calls == [("BTC", 5)]
    home_module._cached_benefit(market, "ETH", 5)
    assert len(calls) == 2


def test_summary_counts_predictions_issued_after_their_window_opened(tmp_path):
    store = SQLiteHotStore(tmp_path / "late.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        monitor.initialize()
        now = datetime.now(UTC).replace(microsecond=0)
        with store.connect() as conn:
            _insert(conn, venue="hyperliquid", issued=now - timedelta(minutes=30))
            conn.execute(
                "UPDATE perp_forward_audit SET issued_at=? WHERE venue='hyperliquid'",
                ((now - timedelta(minutes=24)).isoformat(),),  # target opened at -25
            )
            _insert(conn, venue="dydx", issued=now - timedelta(minutes=30))
        score = monitor.summary(asset="BTC", now=now)["last_7d"][5]
        assert score["issued"] == 2
        assert score["issued_late"] == 1
    finally:
        store.close()
