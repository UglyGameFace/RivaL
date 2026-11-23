"""The settlement sweep's window scans must have their covering indexes.

PolymarketResearch._unsettled reads exactly (expires_at, slug,
condition_id) from each of its three source tables behind a 30-day
expires_at window. The (expires_at, slug, condition_id) covering
indexes keep those branches index-only instead of hauling the wide
evidence heap on every 60 s pass (up to 273 s under load, 2026-11-23).
The sweep's rows and their (checked_at, expires_at) order are pinned
by the second test.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.polymarket_research import PolymarketResearch

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
COND = "0x" + "a" * 64


def _index_exists(store, table: str, name: str) -> bool:
    with store.connect() as conn:
        if store.database.backend == "postgres":
            row = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename=? AND indexname=?",
                (table, name),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
                (table, name),
            ).fetchone()
    return row is not None


def test_unsettled_covering_indexes_declared(learning_store):
    PolymarketResearch(learning_store).initialize()
    assert _index_exists(
        learning_store, "rival_polymarket_ai_calls",
        "idx_rival_polymarket_ai_calls_expiry_cover")
    assert _index_exists(
        learning_store, "rival_polymarket_observations",
        "idx_rival_poly_observations_expiry_cover")
    assert _index_exists(
        learning_store, "rival_poly_contest_rounds",
        "idx_rival_poly_contest_rounds_expiry_cover")


def _seed_sweep(store) -> None:
    def iso(hours_ago: float) -> str:
        return (NOW - timedelta(hours=hours_ago)).isoformat()

    with store.connect() as conn:
        # A: observation, expired 1h ago, unsettled, never attempted.
        conn.execute(
            "INSERT INTO rival_polymarket_observations "
            "(slug,condition_id,asset,horizon,resolution_source,expires_at,"
            "observed_at,features_json,evidence_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("a-open", COND, "BTC", 5, "src", iso(1), iso(2), "{}", "ev-a"),
        )
        # The identical triple also sits in contest rounds: dedupe keeps one.
        conn.execute(
            "INSERT INTO rival_poly_contest_rounds "
            "(slug,condition_id,asset,horizon,schema_key,opens_at,expires_at,"
            "issued_at,quote_at,market_up,evidence_sha256,evidence_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a-open", COND, "BTC", 5, "k", iso(2), iso(1), iso(2), iso(2),
             0.5, "ev-a", "{}"),
        )
        # B: observation with an outcome already recorded: excluded.
        conn.execute(
            "INSERT INTO rival_polymarket_observations "
            "(slug,condition_id,asset,horizon,resolution_source,expires_at,"
            "observed_at,features_json,evidence_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("b-settled", COND, "BTC", 5, "src", iso(2), iso(3), "{}", "ev-b"),
        )
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes "
            "(slug,condition_id,direction,resolved_at,observed_at,"
            "receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?)",
            ("b-settled", COND, "UP", iso(0), iso(0), "r-b", "{}"),
        )
        # D: AI call expired 3h ago, unsettled, but attempted before, so
        # it sorts after never-attempted A despite expiring earlier.
        conn.execute(
            "INSERT INTO rival_polymarket_ai_calls "
            "(slug,asset,horizon,condition_id,opens_at,expires_at,issued_at,"
            "model,direction,thesis,risk,evidence_sha256,evidence_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("d-attempted", "BTC", 5, COND, iso(4), iso(3), iso(4),
             "m", "UP", "t", "r", "ev-d", "{}"),
        )
        conn.execute(
            "INSERT INTO rival_polymarket_resolution_attempts (slug,checked_at) "
            "VALUES (?,?)",
            ("d-attempted", iso(0)),
        )
        # E: outside the 30-day window: excluded.
        conn.execute(
            "INSERT INTO rival_polymarket_observations "
            "(slug,condition_id,asset,horizon,resolution_source,expires_at,"
            "observed_at,features_json,evidence_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("e-ancient", COND, "BTC", 5, "src",
             (NOW - timedelta(days=31)).isoformat(), iso(2), "{}", "ev-e"),
        )


def test_unsettled_rows_and_order_pinned(learning_store):
    research = PolymarketResearch(learning_store)
    research.initialize()
    _seed_sweep(learning_store)
    rows = research._unsettled(NOW)
    assert [(r["slug"], r["condition_id"], r["expires_at"]) for r in rows] == [
        ("a-open", COND, (NOW - timedelta(hours=1)).isoformat()),
        ("d-attempted", COND, (NOW - timedelta(hours=3)).isoformat()),
    ]
