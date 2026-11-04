"""Historical logistic candidates enter source-exact forward quote research.

All generated candles, samples and quotes in these tests are synthetic fixtures.
"""
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.opportunities import recent_watches
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_family_cost_promotion import _seed


def _candidate(stamp):
    return {
        "identity": {"venue": "hyperliquid", "contract": "BTC", "quote": "USDC"},
        "horizon_minutes": 5,
        "model_sha256": "a" * 64,
        "training_sha256": "b" * 64,
        "training_count": 510,
        "imported_at": (stamp - timedelta(days=2)).isoformat(),
        "evaluation_completed_through": (stamp - timedelta(days=3)).isoformat(),
        "probability_up": .91,
    }


def _issue(store, stamp, candidate):
    monitor = ShadowModelCompetition(store)
    monitor.initialize()
    with store.connect() as conn:
        return monitor.issue(
            conn, samples=[], features=(0.,) * 10, now=stamp,
            venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
            horizon=5, reference_opened_at=stamp,
            target_opened_at=stamp + timedelta(minutes=5),
            issued_at=stamp, incumbent_up=.52, baseline_up=.50,
            historical_candidates=(candidate,),
        )


def test_historical_candidate_enters_quote_shadow_without_changing_incumbent(tmp_path):
    store = SQLiteHotStore(tmp_path / "historical-quote.sqlite")
    stamp = datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert _issue(store, stamp, _candidate(stamp)) == 1

    with store.connect() as conn:
        row = conn.execute("SELECT * FROM rival_model_shadow_forecasts").fetchone()
    assert row["family"] == "historical_" + "a" * 48
    assert row["training_sha256"] == "b" * 64
    assert row["training_count"] == 510
    assert row["probability_up"] == .91
    assert row["incumbent_up"] == .52
    assert row["settled_at"] is None

    entry = stamp + timedelta(seconds=20)
    book = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=100., ask=100.1,
        bid_quantity=20., ask_quantity=20.,
        exchange_at=entry, received_at=entry,
    )
    observer = FamilyQuoteShadow(store)
    assert observer.observe(book, now=entry + timedelta(seconds=1))["opened"] == 1
    with store.connect() as conn:
        quoted = conn.execute("SELECT * FROM rival_family_quote_shadow").fetchone()
    assert quoted["family"] == row["family"]
    assert quoted["training_sha256"] == row["training_sha256"]

    proof = FamilyPromotion(store).evaluate(
        asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
        horizon=5, now=stamp + timedelta(minutes=1),
    )
    assert proof["status"] == "held"
    assert proof["selected_family"] is None
    store.close()


def test_invalid_or_wrong_source_historical_candidates_never_issue(tmp_path):
    stamp = datetime(2026, 11, 1, 12, tzinfo=UTC)
    bad_cases = [
        {"identity": {"venue": "okx", "contract": "BTC", "quote": "USDC"}},
        {"horizon_minutes": 15},
        {"imported_at": (stamp + timedelta(minutes=1)).isoformat()},
        {"evaluation_completed_through": stamp.isoformat()},
        {"model_sha256": "not-a-valid-sha"},
        {"training_sha256": "not-a-valid-sha"},
        {"probability_up": float("nan")},
        {"probability_up": 1.1},
    ]
    for i, bad in enumerate(bad_cases):
        store = SQLiteHotStore(tmp_path / f"bad-historical-{i}.sqlite")
        assert _issue(store, stamp, {**_candidate(stamp), **bad}) == 0
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts"
            ).fetchone()["n"] == 0
        store.close()


def test_historical_paper_winner_cannot_serve_without_imported_artifact(learning_store):
    stamp = datetime(2026, 11, 1, 16, tzinfo=UTC)
    original_family = _seed(learning_store, stamp)
    historical_family = "historical_" + "a" * 48
    # Independently future-issued, matched and settled synthetic paper receipts
    # test the governance path, never a model's actual trading skill.
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_model_shadow_forecasts SET family=? WHERE family=?",
            (historical_family, original_family),
        )
        conn.execute(
            "UPDATE rival_family_quote_shadow SET family=? WHERE family=?",
            (historical_family, original_family),
        )
    promoter = FamilyPromotion(learning_store)
    proof = promoter.evaluate(
        asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
        horizon=5, now=stamp,
    )
    assert proof["families"][historical_family]["qualified"]
    assert proof["selected_family"] == historical_family
    assert promoter.selected(
        asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
        horizon=5, now=stamp,
    ) is None


def test_qualified_historical_watch_auto_serves_only_with_current_proof(
    tmp_path, monkeypatch,
):
    """Historical paper approval can replace one source-matched watch, not rows."""
    store = SQLiteHotStore(tmp_path / "historical-watch.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    opened = now - timedelta(minutes=5)
    observed = now + timedelta(milliseconds=200)
    issued = now + timedelta(seconds=1)
    candle = MarketCandle(
        "hyperliquid", "BTC", "USDC", 5, opened,
        now - timedelta(milliseconds=1), observed,
        100., 101., 99., 100.5, 4., 100,
    )
    market.ingest(MarketBatch((), (candle,), 5, observed))
    market.record_feed_health(
        venue="hyperliquid", asset="BTC", status="ok",
        attempted_at=observed, new_candles=1, total_candles=1,
        last_candle_close=candle.closed_at,
    )
    ForwardAccuracyMonitor(market).initialize()
    FamilyPromotion(store).initialize()
    family = "historical_" + "a" * 48
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
            (venue,contract,quote,asset,horizon_minutes,model_version,
             reference_opened_at,reference_close,issued_at,target_opened_at,
             probability_up,baseline_up,training_evaluated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
             opened.isoformat(), 100.5, issued.isoformat(), now.isoformat(),
             .52, .5, 100),
        )
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts
            (venue,contract,quote,asset,horizon_minutes,family,
             reference_opened_at,issued_at,target_opened_at,
             probability_up,incumbent_up,baseline_up,
             training_count,training_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5, family,
             opened.isoformat(), issued.isoformat(), now.isoformat(),
             .91, .52, .5, 510, "b" * 64),
        )
        conn.execute(
            """INSERT INTO rival_family_promotion
            (venue,contract,quote,asset,horizon_minutes,evaluated_at,
             selected_family,status,reason,evidence_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5,
             now.isoformat(), family, "promoted_paper", "fixture", "{}"),
        )
    monkeypatch.setattr(
        FamilyPromotion, "selected", lambda self, **kwargs: family,
    )
    watches = recent_watches(market, asset="BTC", now=now + timedelta(seconds=2))
    assert len(watches) == 1
    assert watches[0]["model_version"] == family
    assert watches[0]["probability_up"] == .91
    assert watches[0]["training_evaluated"] == 510
    # A revoked/unavailable artifact must immediately restore the incumbent
    # without editing historical probabilities or using stale promotion rows.
    monkeypatch.setattr(
        FamilyPromotion, "selected", lambda self, **kwargs: None,
    )
    reverted = recent_watches(market, asset="BTC", now=now + timedelta(seconds=2))
    assert len(reverted) == 1
    assert reverted[0]["model_version"] == MODEL
    assert reverted[0]["probability_up"] == .52
    store.close()


def test_flow_challenger_forecasts_get_source_matched_quotes(tmp_path):
    """Every shadow family the coverage check counts must be quote-shadowed."""
    store = SQLiteHotStore(tmp_path / "flow-quote.sqlite")
    stamp = datetime(2026, 11, 1, 12, tzinfo=UTC)
    monitor = ShadowModelCompetition(store)
    monitor.initialize()
    with store.connect() as conn:
        assert monitor.issue(
            conn, samples=[], features=(0.,) * 10, now=stamp,
            venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
            horizon=5, reference_opened_at=stamp,
            target_opened_at=stamp + timedelta(minutes=5),
            issued_at=stamp, incumbent_up=.52, baseline_up=.50,
            challengers=({"family": "flow_v2", "probability": .62,
                          "training_rows": 300, "model_sha256": "c" * 64},),
        ) == 1
    entry = stamp + timedelta(seconds=20)
    book = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=100., ask=100.1,
        bid_quantity=20., ask_quantity=20., exchange_at=entry, received_at=entry,
    )
    assert FamilyQuoteShadow(store).observe(book, now=entry + timedelta(seconds=1))["opened"] == 1
    with store.connect() as conn:
        quoted = conn.execute("SELECT family, challenger_side FROM rival_family_quote_shadow").fetchone()
    assert quoted["family"] == "flow_v2" and quoted["challenger_side"] == "LONG"
    store.close()
