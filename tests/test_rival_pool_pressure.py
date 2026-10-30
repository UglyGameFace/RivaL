"""Regression coverage for the shared PostgreSQL pool pressure incident."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import auto_hunt, opportunities, signals
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.database import PostgresDatabase
from parlay_bot.storage.hot import SQLiteHotStore


def test_market_schema_is_not_recreated_for_each_candle_read(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "market.db")
    market = PerpetualMarketStore(store)
    original = store.connect
    calls = []

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setattr(store, "connect", counted)
    market.initialize()
    market.initialize()
    market.initialize()
    assert len(calls) == 1
    with original() as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='perp_market_candles'"
        ).fetchone() is not None


def test_forward_read_schema_initialized_only_once_across_threads(tmp_path, monkeypatch):
    market = PerpetualMarketStore(SQLiteHotStore(tmp_path / "market.db"))
    calls = []

    def initialize(_self):
        calls.append(1)

    monkeypatch.setattr(opportunities.ForwardAccuracyMonitor, "_initialize_schema", initialize)
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(
            lambda _: opportunities.ForwardAccuracyMonitor(market).initialize(),
            range(12),
        ))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_background_signal_uses_matching_watch_gate_without_full_reports(
    monkeypatch,
):
    stamp = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    watch = {
        "venue": "hyperliquid", "horizon_minutes": 15,
        "probability_up": 0.65, "issued": stamp - timedelta(seconds=15),
        "target": stamp + timedelta(minutes=4),
        "model_version": "test-model",
    }
    monkeypatch.setattr(signals, "promoted_engine", lambda *_a, **_kw: "live")
    monkeypatch.setattr(signals, "recent_watches", lambda *_a, **_kw: [watch])
    monkeypatch.setattr(
        signals, "_venue_proof",
        lambda *_a, **_kw: {
            "settled": 70, "hit_rate": 0.60, "model_beats_baseline": True,
        },
    )

    def should_not_run(*_a, **_kw):
        raise AssertionError("full portfolio proof should not run in background")

    monkeypatch.setattr(signals, "_proof", should_not_run)

    class Quote:
        bid = 100.0
        ask = 100.1
        exchange_at = stamp - timedelta(seconds=2)

        def validate(self, *, now):
            assert now == stamp

    async def venue_book(*_a, **_kw):
        return Quote()

    monkeypatch.setattr(signals, "_venue_book", venue_book)
    decision = {}
    await signals.signal_embed(
        SimpleNamespace(store=object()), object(),
        asset="BTC", horizon_minutes=15, now=stamp,
        decision_out=decision, decision_only=True,
    )
    assert decision["is_watch"] is True
    assert decision["venue"] == "hyperliquid"
    assert decision["probability"] == 0.65
    assert decision["quote_at"] == Quote.exchange_at
    assert decision["after_cost_bps"] is None


@pytest.mark.asyncio
async def test_forecast_alert_snapshots_one_live_read_per_asset(monkeypatch):
    stamp = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    seen: dict[str, int] = {}

    def recent(_market, *, asset, **_kw):
        seen[asset] = seen.get(asset, 0) + 1
        return [
            {
                "horizon_minutes": horizon, "issued": stamp,
                "target": stamp + timedelta(minutes=4),
                "probability_up": 0.64, "venue": "hyperliquid",
            }
            for horizon in auto_hunt.HORIZONS
        ]

    monkeypatch.setattr(auto_hunt, "recent_watches", recent)
    monkeypatch.setattr(
        auto_hunt, "promoted_engine", lambda *_a, **_kw: "live",
    )
    forecasts = await auto_hunt.new_forecast_watches(
        SimpleNamespace(store=object()), now=stamp,
    )
    assert len(forecasts) == len(auto_hunt.ASSETS) * len(auto_hunt.HORIZONS)
    assert seen == {asset: 1 for asset in auto_hunt.ASSETS}


@pytest.mark.asyncio
async def test_qualified_hunt_requests_lightweight_decision(monkeypatch):
    stamp = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    calls = []

    async def fake_embed(_market, _quotes, *, asset, horizon_minutes,
                         decision_out, decision_only, **_kw):
        assert decision_only is True
        calls.append((asset, horizon_minutes))
        if asset == auto_hunt.ASSETS[0] and horizon_minutes == auto_hunt.HORIZONS[0]:
            decision_out.update({
                "is_watch": True, "asset": asset,
                "horizon_minutes": horizon_minutes,
                "venue": "hyperliquid", "engine": "live",
                "model_version": "test", "issued": stamp,
                "target": stamp + timedelta(minutes=4),
                "direction": "UP", "probability": 0.64,
                "quote_at": stamp, "bid": 100.0, "ask": 100.1,
                "after_cost_bps": None, "after_cost_status": None,
            })

    monkeypatch.setattr(auto_hunt, "signal_embed", fake_embed)
    watches = await auto_hunt.qualified_watches(
        object(), object(), stream_shadow=None, now=stamp,
    )
    assert len(calls) == len(auto_hunt.ASSETS) * len(auto_hunt.HORIZONS)
    assert len(watches) == 1
    assert watches[0].asset == auto_hunt.ASSETS[0]

def test_postgres_pool_releases_connections_after_context_exit(monkeypatch):
    """A saturated pool must recover without increasing the server's limit."""
    from psycopg.pq import TransactionStatus

    class DummyConnection:
        closed = False
        commits = 0
        rollbacks = 0
        info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            # Real psycopg3 Connection.__exit__ closes the socket when _pool
            # is unset; _Connection.__exit__ must commit/rollback directly.
            self.closed = True
            return None

        def commit(self):
            self.commits += 1
            self.info.transaction_status = TransactionStatus.IDLE

        def rollback(self):
            self.rollbacks += 1
            self.info.transaction_status = TransactionStatus.IDLE

        def close(self):
            self.closed = True

    created = []
    def fake_connect(*_args, **_kwargs):
        connection = DummyConnection()
        created.append(connection)
        return connection

    monkeypatch.setattr("parlay_bot.storage.database.psycopg.connect", fake_connect)
    database = PostgresDatabase(
        "postgresql://example.invalid/rival", max_size=1, checkout_timeout=0.01,
    )
    with database.connect(), pytest.raises(TimeoutError, match="pool exhausted"):
        database.connect()
    with database.connect():
        assert len(created) == 1
        assert created[0].commits == 1
        assert not created[0].closed
    with pytest.raises(RuntimeError, match="boom"), database.connect():
        raise RuntimeError("boom")
    assert created[0].rollbacks == 1
    assert not created[0].closed
    database.close()
    assert created[0].closed


@pytest.mark.asyncio
async def test_background_signal_skips_books_for_weak_or_unqualified_forecasts(
    monkeypatch,
):
    stamp = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    weak_watch = {
        "venue": "okx", "horizon_minutes": 15,
        "probability_up": 0.52, "issued": stamp - timedelta(seconds=15),
        "target": stamp + timedelta(minutes=4),
        "model_version": "test-model",
    }
    monkeypatch.setattr(signals, "promoted_engine", lambda *_a, **_kw: "live")
    monkeypatch.setattr(signals, "recent_watches", lambda *_a, **_kw: [weak_watch])

    def fail_if_called(*_a, **_kw):
        raise AssertionError("should not query proof or books for weak forecast")

    monkeypatch.setattr(signals, "_venue_proof", fail_if_called)
    monkeypatch.setattr(signals, "_venue_book", fail_if_called)
    decision = {}
    await signals.signal_embed(
        SimpleNamespace(store=object()), object(),
        asset="BTC", horizon_minutes=15, now=stamp,
        decision_out=decision, decision_only=True,
    )
    assert decision == {}

    strong_watch = {**weak_watch, "probability_up": 0.65}
    monkeypatch.setattr(signals, "recent_watches", lambda *_a, **_kw: [strong_watch])
    monkeypatch.setattr(
        signals, "_venue_proof",
        lambda *_a, **_kw: {
            "settled": 70, "hit_rate": 0.48, "model_beats_baseline": False,
        },
    )
    await signals.signal_embed(
        SimpleNamespace(store=object()), object(),
        asset="BTC", horizon_minutes=15, now=stamp,
        decision_out=decision, decision_only=True,
    )
    assert decision == {}

