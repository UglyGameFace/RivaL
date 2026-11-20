"""Forward rows carry the notice's per-contract fee evidence from issuance.

After-fee figures must be exact per contract, not schedule-based: the
notice's own feeSchedule rate and depth-average asks are persisted on the
forward row at observe() time. Rows issued before this change keep NULL
evidence (never fabricated) and readers fall back to the schedule.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.polymarket_watch import PolymarketNotice
from parlay_bot.perpetuals import polymarket_learning
from parlay_bot.perpetuals.polymarket_learning import MODEL, PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore

OLD_FORWARD_DDL = (
    "CREATE TABLE rival_polymarket_model_forward ("
    "slug TEXT NOT NULL,model_version TEXT NOT NULL,issued_at TEXT NOT NULL,"
    "weights_sha256 TEXT NOT NULL,probability_up DOUBLE PRECISION NOT NULL,"
    "market_baseline DOUBLE PRECISION NOT NULL,"
    "PRIMARY KEY(slug,model_version))"
)


def _notice(**overrides) -> PolymarketNotice:
    now = datetime.now(UTC)
    start = now - timedelta(minutes=1)
    fields = {
        "asset": "BTC", "horizon": 5, "issued": start,
        "expires": now + timedelta(minutes=4),
        "slug": f"btc-updown-5m-{int(start.timestamp())}",
        "up_ask": .52, "down_ask": .49, "quote_at": now,
        "condition_id": "0x" + "1" * 64,
        "up_token_id": "1" * 60, "down_token_id": "2" * 60,
        "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "rules": "Chainlink TWAP 60 seconds",
    }
    fields.update(overrides)
    return PolymarketNotice(**fields)


def _learner(store) -> PolymarketOutcomeLearner:
    research = PolymarketResearch(store)
    research.initialize()  # Shared outcome receipts + the learner's tables.
    return research.learner


def _forward_row(store, slug):
    with store.connect() as conn:
        return conn.execute(
            "SELECT fee_rate,up_average_ask,down_average_ask "
            "FROM rival_polymarket_model_forward WHERE slug=? AND model_version=?",
            (slug, MODEL),
        ).fetchone()


def test_forward_row_carries_the_notice_fee_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(polymarket_learning, "MIN_LABELS", 0)
    store = SQLiteHotStore(tmp_path / "fees.sqlite")
    try:
        learner = _learner(store)
        notice = _notice(fee_rate=0.05, up_average_ask=0.53, down_average_ask=0.50)
        call = learner.observe(notice)
        assert call is not None  # the forward row was actually issued
        row = _forward_row(store, notice.slug)
        assert row is not None
        assert row["fee_rate"] == notice.fee_rate == 0.05
        assert row["up_average_ask"] == notice.up_average_ask == 0.53
        assert row["down_average_ask"] == notice.down_average_ask == 0.50
    finally:
        store.close()


def test_forward_row_without_notice_evidence_stays_null(tmp_path, monkeypatch):
    monkeypatch.setattr(polymarket_learning, "MIN_LABELS", 0)
    store = SQLiteHotStore(tmp_path / "nofees.sqlite")
    try:
        learner = _learner(store)
        notice = _notice()  # fee_rate / average asks default to None
        assert learner.observe(notice) is not None
        row = _forward_row(store, notice.slug)
        assert row is not None
        assert row["fee_rate"] is None
        assert row["up_average_ask"] is None
        assert row["down_average_ask"] is None
    finally:
        store.close()


def test_legacy_forward_table_is_migrated_and_old_rows_read_null(tmp_path, monkeypatch):
    monkeypatch.setattr(polymarket_learning, "MIN_LABELS", 0)
    store = SQLiteHotStore(tmp_path / "legacy.sqlite")
    try:
        with store.connect() as conn:
            conn.execute(OLD_FORWARD_DDL)
            conn.execute(
                "INSERT INTO rival_polymarket_model_forward "
                "(slug,model_version,issued_at,weights_sha256,probability_up,market_baseline) "
                "VALUES (?,?,?,?,?,?)",
                ("legacy-slug", MODEL, datetime.now(UTC).isoformat(), "s", .5, .5),
            )
        learner = _learner(store)  # initialize() runs the additive migration
        legacy = _forward_row(store, "legacy-slug")
        assert legacy is not None
        assert legacy["fee_rate"] is None
        assert legacy["up_average_ask"] is None
        assert legacy["down_average_ask"] is None
        # New issuance into the migrated table persists evidence as usual.
        notice = _notice(fee_rate=0.01, up_average_ask=0.44, down_average_ask=0.57)
        assert learner.observe(notice) is not None
        row = _forward_row(store, notice.slug)
        assert row["fee_rate"] == 0.01
        assert row["up_average_ask"] == 0.44
        assert row["down_average_ask"] == 0.57
        # Migration is idempotent: a second learner initializes cleanly.
        PolymarketOutcomeLearner(store).initialize()
    finally:
        store.close()
