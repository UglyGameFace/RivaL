"""Regression: busy BTC/ETH/SOL 5m streams cannot hide valid 15m forecasts.

Previously latest 40 rows were selected across all horizons, then filtered
in Python. A heavy 5m issuance window could report WAIT for a genuinely
stored 15m forecast. Test against real SQLite SQL, not a mocked SQL string.
"""
from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import opportunities, targeting
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL
from parlay_bot.perpetuals.readiness import _source_identity


@pytest.mark.parametrize("asset", ("BTC", "ETH", "SOL"))
@pytest.mark.parametrize("engine", ("live", "multi"))
def test_selected_horizon_not_starved_by_40_newer_other_horizon_rows(
    monkeypatch, asset, engine,
):
    stamp = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    venue = "hyperliquid"
    contract, quote = _source_identity(venue, asset)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE perp_forward_audit (
            venue TEXT, contract TEXT, quote TEXT, asset TEXT,
            horizon_minutes INTEGER, model_version TEXT,
            issued_at TEXT, target_opened_at TEXT,
            reference_close REAL, probability_up REAL,
            training_evaluated INTEGER, settled_at TEXT
        );
        CREATE TABLE perp_multi_forward (
            asset TEXT, horizon_minutes INTEGER, target_opened_at TEXT,
            issued_at TEXT, model_version TEXT, probability_up REAL,
            reference_venue TEXT, reference_contract TEXT,
            reference_quote TEXT, contributor_count INTEGER,
            settled_at TEXT
        );
    """)
    # All 40 recent forecasts are for 5m, so the older 15m would have
    # vanished under the previous "ORDER BY issued_at DESC LIMIT 40".
    for index in range(41):
        horizon = 5 if index < 40 else 15
        issued = stamp - timedelta(
            seconds=index + 5 if index < 40 else 120,
        )
        probability = 0.54 if horizon == 5 else 0.68
        params = (
            asset, horizon, issued.isoformat(),
            (stamp+timedelta(minutes=5)).isoformat(),
            probability,
        )
        conn.execute(
            "INSERT INTO perp_forward_audit "
            "(venue,contract,quote,asset,horizon_minutes,model_version,"
            "issued_at,target_opened_at,reference_close,probability_up,"
            "training_evaluated,settled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (venue, contract, quote, asset, horizon, "model-v1",
             issued.isoformat(),
             (stamp+timedelta(minutes=5)).isoformat(), 2500.0,
             probability, 600),
        )
        conn.execute(
            "INSERT INTO perp_multi_forward "
            "(asset,horizon_minutes,issued_at,target_opened_at,model_version,"
            "probability_up,reference_venue,reference_contract,"
            "reference_quote,contributor_count,settled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
            (asset, horizon, params[2], params[3], MULTI_MODEL,
             probability, venue, contract, quote, 3),
        )
    conn.commit()
    market = SimpleNamespace(
        store=SimpleNamespace(connect=lambda: nullcontext(conn)),
        feed_health=lambda: [{
            "venue": venue, "asset": asset, "status": "ok",
            "last_success_at": stamp.isoformat(),
        }],
        recent_candles=lambda **_kwargs: [{
            "first_observed_at": (
                stamp-timedelta(seconds=10)
            ).isoformat(),
            "close_price": 2500.0,
        }],
    )
    monkeypatch.setattr(
        ForwardAccuracyMonitor, "initialize", lambda _self: None,
    )
    monkeypatch.setattr(
        targeting, "promoted_engine",
        lambda *_a, **_kw: engine,
    )
    try:
        reader = (
            opportunities.recent_multi_watches
            if engine == "multi" else opportunities.recent_watches
        )
        watches = reader(
            market, asset=asset, now=stamp,
            horizon_minutes=15,
        )
        assert len(watches) == 1
        assert watches[0]["horizon_minutes"] == 15
        assert watches[0]["probability_up"] == 0.68
        actual = targeting.target_snapshot(
            market, asset=asset, horizon_minutes=15, now=stamp,
        )
        assert actual.status == "ok"
        assert actual.direction == "UP"
        assert actual.horizon_minutes == 15
        assert actual.venue == venue
        assert actual.probability == 0.68
        # Unsupported horizons are rejected, never treated as wildcard.
        with pytest.raises(ValueError):
            reader(market, asset=asset, now=stamp, horizon_minutes=7)
    finally:
        conn.close()
