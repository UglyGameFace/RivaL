"""A healthy generic forecast cycle must not hide a silent fitted XMarket scope.

These are synthetic ledger fixtures. Production confirmation still requires the
Discloud watchdog to execute on the authenticated host database.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import (
    REGRESSION_RECHECK,
    RuntimeHealthMonitor,
)
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
from parlay_bot.perpetuals.xmarket_watchdog_receipt import read_xmarket_watchdog
from tests.xmarket_fixtures import valid_doc


def _setup(store, now):
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    ShadowModelCompetition(store).initialize()
    models = XmarketModelSet(store)
    models.initialize()
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
            slot_utc TEXT PRIMARY KEY, started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL, math_issued INTEGER NOT NULL,
            math_settled INTEGER NOT NULL, ai_issued INTEGER NOT NULL,
            math_ok INTEGER NOT NULL, ai_settlement_ok INTEGER NOT NULL,
            model_review_ok INTEGER NOT NULL, auto_ai_ok INTEGER NOT NULL
        )""")
    return models


def _fit(models, store, asset, horizon, now, *, age_hours=5):
    model = valid_doc(venues=(), asset=asset, horizon=horizon, now=now)
    assert models.import_document(
        f"test-{asset}-{horizon}", json.dumps(model).encode()
    ) == 1
    with store.connect() as conn:
        conn.execute(
            "UPDATE rival_xmarket_models SET imported_at=? "
            "WHERE asset=? AND horizon_minutes=?",
            ((now - timedelta(hours=age_hours)).isoformat(), asset, horizon),
        )


def _incumbents(store, asset, horizon, now):
    with store.connect() as conn:
        for i in range(10):
            issued = now - timedelta(minutes=110 - 10 * i)
            reference = issued - timedelta(minutes=5)
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", asset, "USDC", asset, horizon, MODEL,
                 reference.isoformat(), 100.0, issued.isoformat(),
                 (issued + timedelta(minutes=horizon)).isoformat(), .60, .50, 100),
            )


def _shadow(store, asset, horizon, now):
    issued = now - timedelta(minutes=35)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts
               (venue,contract,quote,asset,horizon_minutes,family,
                reference_opened_at,issued_at,target_opened_at,probability_up,
                incumbent_up,baseline_up,training_count,training_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", asset, "USDC", asset, horizon, "xmarket_v1",
             (issued - timedelta(minutes=5)).isoformat(), issued.isoformat(),
             (issued + timedelta(minutes=horizon)).isoformat(), .62, .60, .50,
             1000, "a" * 64),
        )


def _monitor(store, now):
    monitor = RuntimeHealthMonitor(store)
    monitor.started_at = now - timedelta(hours=4)
    monitor._regression_cache = (now, {})
    return monitor


def _inspect(monitor, now):
    return monitor.inspect(
        now=now, learning_enabled=True, import_enabled=True,
        regressions_inline=False,
    )


def test_xmarket_watchdog_detects_silent_qualified_scope_not_other_scopes(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    models = _setup(learning_store, now)
    _fit(models, learning_store, "BTC", 15, now)
    _fit(models, learning_store, "XRP", 60, now)
    _incumbents(learning_store, "BTC", 15, now)
    _incumbents(learning_store, "XRP", 60, now)
    _shadow(learning_store, "XRP", 60, now)

    monitor = _monitor(learning_store, now)
    faults = _inspect(monitor, now)
    assert "xmarket:no_shadows:BTC:15" in faults
    assert "10 recent incumbent forecasts" in faults["xmarket:no_shadows:BTC:15"]
    assert "xmarket:no_shadows:XRP:60" not in faults
    assert "xmarket:no_shadows:BTC:5" not in faults
    assert all(not key.startswith("xmarket:no_shadows:ETH") for key in faults)

    # A newly prospective shadow is persisted; the watchdog's bounded
    # 10-minute recheck should clear the stale incident, not restart anything.
    _shadow(learning_store, "BTC", 15, now)
    assert "xmarket:no_shadows:BTC:15" in _inspect(
        monitor, now + timedelta(minutes=2))
    assert "xmarket:no_shadows:BTC:15" not in _inspect(
        monitor, now + REGRESSION_RECHECK + timedelta(seconds=1))


def test_xmarket_watchdog_respects_fresh_import_and_no_incumbent_data(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    models = _setup(learning_store, now)
    _fit(models, learning_store, "BTC", 15, now, age_hours=1)
    _fit(models, learning_store, "XRP", 60, now)
    _incumbents(learning_store, "BTC", 15, now)
    monitor = _monitor(learning_store, now)
    faults = _inspect(monitor, now)
    assert not any(key.startswith("xmarket:no_shadows:") for key in faults)

    # Once an imported fit has had enough time AND the source continues
    # to produce incumbents, missing XMarket shadows are an operational gap.
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_xmarket_models SET imported_at=? "
            "WHERE asset='BTC' AND horizon_minutes=15",
            ((now - timedelta(hours=3)).isoformat(),),
        )
    fresh_monitor = _monitor(learning_store, now)
    assert "xmarket:no_shadows:BTC:15" in _inspect(fresh_monitor, now)


def test_xmarket_watchdog_cold_host_does_not_initialize_optional_models(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    monitor = _monitor(learning_store, now)
    with learning_store.connect() as conn:
        assert monitor._xmarket_issuance_faults(conn, now) == {}
        if learning_store.database.backend == "postgres":
            found = conn.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema=current_schema() "
                "AND table_name='rival_xmarket_models'"
            ).fetchone()["n"]
        else:
            found = conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name='rival_xmarket_models'"
            ).fetchone()["n"]
    assert found == 0


def test_host_receipt_proves_real_scope_checks_cache_age_and_recovery(learning_store):
    """A clean incident list alone is insufficient; actual published checks count."""
    now = datetime.now(UTC).replace(microsecond=0)
    models = _setup(learning_store, now)
    for asset, horizon in (("BTC", 15), ("XRP", 60)):
        _fit(models, learning_store, asset, horizon, now)
        _incumbents(learning_store, asset, horizon, now)
    _shadow(learning_store, "XRP", 60, now)
    monitor = _monitor(learning_store, now)
    monitor.initialize()
    assert read_xmarket_watchdog(learning_store, now)["status"] == "not_recorded"

    # Inspect is SELECT-only. A partial pass must not publish successful checks.
    faults = _inspect(monitor, now)
    assert read_xmarket_watchdog(learning_store, now)["status"] == "not_recorded"
    events = monitor.publish(faults, now=now)
    assert ("OPEN", "xmarket:no_shadows:BTC:15") in [e[:2] for e in events]
    report = read_xmarket_watchdog(learning_store, now)
    assert report["status"] == "fresh"
    assert report["operational_status"] == "degraded"
    assert report["fitted_scopes"] == 2
    assert report["issuing_scopes"] == report["silent_scopes"] == 1
    assert report["open_silence_incidents"] == 1
    assert report["read_only"] and report["does_not_prove_prediction_edge"]
    assert report["window_start"] == (now - timedelta(hours=2)).isoformat()
    assert report["window_end"] == (now - timedelta(minutes=10)).isoformat()
    assert report["scopes"] == [
        {"asset": "BTC", "horizon_minutes": 15, "status": "silent",
         "incumbent_rows": 10, "shadow_rows": 0},
        {"asset": "XRP", "horizon_minutes": 60, "status": "issuing",
         "incumbent_rows": 10, "shadow_rows": 1},
    ]

    _shadow(learning_store, "BTC", 15, now)
    cached_at = now + timedelta(minutes=2)
    assert monitor.publish(_inspect(monitor, cached_at), now=cached_at) == []
    cached = read_xmarket_watchdog(learning_store, cached_at)
    assert cached["scope_checked_at"] == report["scope_checked_at"]
    assert cached["scope_age_seconds"] == 120
    assert cached["operational_status"] == "degraded"
    checked_at = now + REGRESSION_RECHECK + timedelta(seconds=1)
    recovered_events = monitor.publish(_inspect(monitor, checked_at), now=checked_at)
    assert ("RESOLVED", "xmarket:no_shadows:BTC:15") in [e[:2] for e in recovered_events]
    recovered = read_xmarket_watchdog(learning_store, checked_at)
    assert recovered["status"] == "fresh"
    assert recovered["operational_status"] == "healthy"
    assert recovered["issuing_scopes"] == 2
    assert recovered["open_silence_incidents"] == 0
    assert recovered["resolved_silence_incidents"] == 1
    assert recovered["incidents"][0]["occurrence_count"] == 1
    assert recovered["incidents"][0]["resolved_at"] == checked_at.isoformat()


def test_host_receipt_never_calls_warmup_or_no_incumbents_healthy(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    models = _setup(learning_store, now)
    _fit(models, learning_store, "BTC", 15, now, age_hours=1)
    _fit(models, learning_store, "SOL", 15, now)
    _incumbents(learning_store, "BTC", 15, now)
    monitor = _monitor(learning_store, now)
    monitor.publish(_inspect(monitor, now), now=now)
    report = read_xmarket_watchdog(learning_store, now)
    assert report["status"] == "fresh"
    assert report["operational_status"] == "pending"
    assert [r["status"] for r in report["scopes"]] == [
        "import_grace", "insufficient_incumbents",
    ]
    assert report["open_silence_incidents"] == 0


def test_host_receipt_rejects_old_boot_future_clock_and_stale_checks(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    _setup(learning_store, now)
    monitor = _monitor(learning_store, now)
    monitor.publish(_inspect(monitor, now), now=now)
    assert read_xmarket_watchdog(learning_store, now)["operational_status"] == "pending"
    # Even a fresh heartbeat cannot make an old scope census fresh again.
    stale_at = now + timedelta(minutes=31)
    monitor.publish({}, now=stale_at)
    assert read_xmarket_watchdog(learning_store, stale_at)["operational_status"] == "unverified"
    assert read_xmarket_watchdog(learning_store, now - timedelta(seconds=1))[
        "operational_status"
    ] == "unverified"
    # A new process publishing generic health did not execute the scope check.
    replacement = RuntimeHealthMonitor(learning_store)
    replacement.started_at = now + timedelta(seconds=30)
    replacement.publish({}, now=now + timedelta(minutes=1))
    report = read_xmarket_watchdog(learning_store, now + timedelta(minutes=1))
    assert report["status"] == "stale_or_boot_unverified"
    assert report["operational_status"] == "unverified"


def test_scope_receipt_is_select_only_and_cold_host_stays_uninitialized(learning_store):
    from contextlib import contextmanager
    from types import SimpleNamespace

    now = datetime.now(UTC).replace(microsecond=0)
    reads = []

    @contextmanager
    def connect():
        with learning_store.connect() as conn:
            class ReadOnly:
                def execute(self, sql, params=()):
                    reads.append(sql)
                    assert sql.lstrip().upper().startswith("SELECT ")
                    return conn.execute(sql, params)
            yield ReadOnly()

    reader = SimpleNamespace(connect=connect)
    assert read_xmarket_watchdog(reader, now)["status"] == "read_unavailable"
    monitor = RuntimeHealthMonitor(learning_store)
    monitor.initialize()
    assert read_xmarket_watchdog(reader, now)["status"] == "not_recorded"
    with learning_store.connect() as conn:
        monitor._xmarket_issuance_faults(conn, now)
    monitor.publish({}, now=now)
    report = read_xmarket_watchdog(reader, now)
    assert report["census_status"] == "not_initialized"
    assert report["operational_status"] == "unverified"
    assert reads
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_runtime_diagnostics SET payload_json=? "
            "WHERE component='xmarket_issuance'", ("not json",),
        )
    assert read_xmarket_watchdog(reader, now)["status"] == "read_unavailable"
