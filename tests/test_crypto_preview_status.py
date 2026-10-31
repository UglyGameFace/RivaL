"""Stage 2: the crypto page preview says which no-signal state it is in.

The old fallback ("NO CURRENT SIGNAL • Preview pending") made a timeout, an
empty forecast book, a tied model, a down collector, and a failed feed check
all read as a broken bot. target_snapshot already distinguishes those states;
the crypto render used to discard that detail. These tests pin each branch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.home import _crypto_preview_headline
from parlay_bot.discord_app.targeting import TargetSnapshot, target_snapshot
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def _market(tmp_path, now, probability_up=None):
    """A real store with one fresh candle, feed health, and optionally one forecast."""
    store = SQLiteHotStore(tmp_path / "preview.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    closed = now - timedelta(minutes=1)
    opened = closed - timedelta(minutes=5) + timedelta(milliseconds=1)
    candle = MarketCandle(
        "gate", "BTC_USDT", "USDT", 5, opened, closed,
        closed + timedelta(milliseconds=1),
        104, 108, 101, 106, 10.0, 34,
    )
    market.ingest(MarketBatch(
        (), (candle,), 5, closed + timedelta(milliseconds=1),
    ))
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=closed,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    ForwardAccuracyMonitor(market).initialize()
    if probability_up is not None:
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "gate", "BTC_USDT", "USDT", "BTC", 5, MODEL,
                    opened.isoformat(), 106.0,
                    (now - timedelta(seconds=45)).isoformat(),
                    (now + timedelta(minutes=2)).isoformat(),
                    probability_up, 0.5, 120,
                ),
            )
    return market


def test_snapshot_status_collector_down():
    snap = target_snapshot(None, asset="BTC", horizon_minutes=5)
    assert snap.status == "collector_down"
    assert snap.direction == "WAIT"
    assert snap.probability is None


def test_snapshot_status_no_forecast(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    snap = target_snapshot(
        _market(tmp_path, now), asset="BTC", horizon_minutes=5, now=now,
    )
    assert snap.status == "no_forecast"
    assert snap.direction == "WAIT"
    assert snap.probability is None


def test_snapshot_status_tied(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    snap = target_snapshot(
        _market(tmp_path, now, probability_up=0.5),
        asset="BTC", horizon_minutes=5, now=now,
    )
    assert snap.status == "tied"
    assert snap.direction == "WAIT"
    assert snap.probability is None


def test_snapshot_status_feed_error(tmp_path, monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)
    market = _market(tmp_path, now, probability_up=0.68)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "parlay_bot.discord_app.targeting.recent_watches", _boom,
    )
    monkeypatch.setattr(
        "parlay_bot.discord_app.targeting.recent_multi_watches", _boom,
    )
    snap = target_snapshot(market, asset="BTC", horizon_minutes=5, now=now)
    assert snap.status == "feed_error"
    assert snap.direction == "WAIT"
    assert snap.probability is None


def test_raw_probabilities_do_not_claim_verified_future_accuracy(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    snap = target_snapshot(
        _market(tmp_path, now, probability_up=0.68),
        asset="BTC", horizon_minutes=5, now=now,
    )
    assert snap.status == "unqualified_model"
    assert snap.direction == "WAIT"
    assert snap.probability is None
    assert "NO CALL" in snap.detail


def test_status_does_not_change_follow_fingerprint():
    base = TargetSnapshot("BTC", 5, "WAIT", "No feed.")
    assert base.fingerprint == TargetSnapshot(
        "BTC", 5, "WAIT", "No feed.", status="no_forecast",
    ).fingerprint


def test_headline_timeout():
    assert _crypto_preview_headline(None) == (
        "Preview timed out — Get Signal checks live"
    )


def test_headline_ok_keeps_lean_format():
    snap = TargetSnapshot(
        "BTC", 5, "UP", "detail", probability=0.68, status="ok",
    )
    assert _crypto_preview_headline(snap) == (
        "UP lean • raw 68.0% (directional watch)"
    )


def test_headline_each_wait_state():
    cases = {
        "tied": "No directional call — model tied",
        "no_forecast": "No current forecast — markets being collected",
        "collector_down": "Collector unavailable — signals paused",
        "feed_error": "Feed check failed — Get Signal verifies live",
    }
    for status, expected in cases.items():
        snap = TargetSnapshot("BTC", 5, "WAIT", "detail", status=status)
        assert _crypto_preview_headline(snap) == expected


def test_headline_unknown_status_falls_back_to_detail():
    snap = TargetSnapshot("BTC", 5, "WAIT", "Something specific.", status="weird")
    assert _crypto_preview_headline(snap) == "Something specific."
