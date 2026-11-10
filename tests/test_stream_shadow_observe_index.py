"""Covering index for StreamShadowLedger.observe()'s audit read.

The slow read is the SELECT in stream_shadow.py observe() (~lines
136-142): five equality filters (asset, venue, contract, quote,
model_version) plus an issued_at range, ORDER BY issued_at ASC
LIMIT 24. Before this index the best usable prefix was the PK's
(venue, contract, quote) — horizon_minutes sits between quote and
model_version in the PK, so model_version and issued_at were
unreachable — and every matching row had to be sorted. The index
added in ForwardAccuracyMonitor._initialize_schema serves that
query exactly.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

# The observe() SELECT, copied verbatim from
# src/parlay_bot/perpetuals/stream_shadow.py (lines ~136-142). If that
# query changes, this test's copy must change with it.
OBSERVE_SQL = """SELECT asset,venue,contract,quote,horizon_minutes,
                        model_version,issued_at,target_opened_at,probability_up
                 FROM perp_forward_audit
                 WHERE asset=? AND venue=? AND contract=? AND quote=?
                   AND model_version=? AND issued_at<=? AND issued_at>=?
                 ORDER BY issued_at ASC LIMIT 24"""

EXPECTED_COLUMNS = [
    "asset",
    "venue",
    "contract",
    "quote",
    "model_version",
    "issued_at",
]


def _make_monitor(tmp_path):
    store = SQLiteHotStore(tmp_path / "shadow_index.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    return store, monitor


def _seed_audit_row(conn, *, venue, asset, horizon, model, ref, issued,
                    quote="USDC"):
    conn.execute(
        """INSERT INTO perp_forward_audit
           (venue,contract,quote,asset,horizon_minutes,model_version,
            reference_opened_at,reference_close,issued_at,target_opened_at,
            probability_up,baseline_up,training_evaluated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (venue, asset, quote, asset, horizon, model, ref.isoformat(), 100.0,
         issued.isoformat(), ref.isoformat(), 0.6, 0.5, 1),
    )


def test_shadow_observe_index_exists_with_exact_columns(tmp_path):
    store, monitor = _make_monitor(tmp_path)
    with store.connect() as conn:
        rows = conn.execute(
            "PRAGMA index_info(idx_perp_forward_shadow_observe)"
        ).fetchall()
    assert [row["name"] for row in rows] == EXPECTED_COLUMNS
    # Idempotent: the DDL runs again cleanly (IF NOT EXISTS), which is
    # what the second process start after deploy relies on.
    monitor._initialize_schema()
    with store.connect() as conn:
        rows = conn.execute(
            "PRAGMA index_info(idx_perp_forward_shadow_observe)"
        ).fetchall()
    assert [row["name"] for row in rows] == EXPECTED_COLUMNS


def test_observe_query_plan_uses_shadow_observe_index(tmp_path):
    store, _ = _make_monitor(tmp_path)
    now = datetime.now(UTC)
    with store.connect() as conn:
        for i in range(300):
            asset = ("BTC", "ETH", "SOL")[i % 3]
            venue = ("hyperliquid", "okx")[i % 2]
            model = MODEL if i % 2 == 0 else "other-model-v1"
            _seed_audit_row(
                conn, venue=venue, asset=asset, horizon=5, model=model,
                ref=now - timedelta(minutes=5 * (i % 50), seconds=i),
                issued=now - timedelta(minutes=i % 90),
            )
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + OBSERVE_SQL,
            ("BTC", "hyperliquid", "BTC", "USDC", MODEL,
             now.isoformat(), (now - timedelta(minutes=60)).isoformat()),
        ).fetchall()
    detail = "\n".join(str(row[3]) for row in plan)
    assert "USING INDEX idx_perp_forward_shadow_observe" in detail
    assert "SCAN perp_forward_audit" not in detail


def test_observe_query_returns_matching_rows_in_issued_order(tmp_path):
    store, _ = _make_monitor(tmp_path)
    now = datetime.now(UTC)
    with store.connect() as conn:
        # 30 matching rows inside the window, plus decoys: another
        # asset, another venue, another model, and rows outside the
        # issued_at window.
        for i in range(30):
            _seed_audit_row(
                conn, venue="hyperliquid", asset="BTC", horizon=5,
                model=MODEL, ref=now - timedelta(minutes=5, seconds=i),
                issued=now - timedelta(minutes=59 - i),
            )
        _seed_audit_row(conn, venue="hyperliquid", asset="ETH", horizon=5,
                        model=MODEL, ref=now - timedelta(minutes=5, seconds=100),
                        issued=now - timedelta(minutes=10))
        _seed_audit_row(conn, venue="okx", asset="BTC", horizon=5,
                        model=MODEL, ref=now - timedelta(minutes=5, seconds=101),
                        issued=now - timedelta(minutes=10))
        _seed_audit_row(conn, venue="hyperliquid", asset="BTC", horizon=5,
                        model="other-model-v1",
                        ref=now - timedelta(minutes=5, seconds=102),
                        issued=now - timedelta(minutes=10))
        _seed_audit_row(conn, venue="hyperliquid", asset="BTC", horizon=5,
                        model=MODEL, ref=now - timedelta(minutes=5, seconds=103),
                        issued=now - timedelta(minutes=120))
        rows = conn.execute(
            OBSERVE_SQL,
            ("BTC", "hyperliquid", "BTC", "USDC", MODEL,
             now.isoformat(), (now - timedelta(minutes=60)).isoformat()),
        ).fetchall()
    # LIMIT 24 of the 30 matching rows, oldest issued_at first.
    assert len(rows) == 24
    issued = [row["issued_at"] for row in rows]
    assert issued == sorted(issued)
    assert all(row["asset"] == "BTC" for row in rows)
    assert all(row["venue"] == "hyperliquid" for row in rows)
    assert all(row["model_version"] == MODEL for row in rows)
