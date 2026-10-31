"""Unattended, production-ledger-shaped acceptance without Discord interactions.

Synthetic fixtures test causality, storage and replay safety. The separate
hosted acceptance receipt MUST still pass on actual live process data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.perpetuals.unattended_acceptance import unattended_acceptance_receipt
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def _book(at: datetime, *, bid: float = 100.0, ask: float = 100.1):
    return PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=bid, ask=ask,
        bid_quantity=100, ask_quantity=100,
        exchange_at=at, received_at=at,
    )


def _samples(at: datetime):
    data = []
    for i in range(140):
        label = int(i % 4 != 0)
        issued = at-timedelta(minutes=5*(150-i))
        data.append(LabeledExample(
            venue="hyperliquid", contract="BTC", quote="USDC",
            horizon_minutes=5, issued_at=issued,
            label_known_at=issued+timedelta(minutes=5),
            features=(.3 if label else -.3,)*10, label_up=label,
        ))
    return data


def _run_acceptance(store, now):
    market = PerpetualMarketStore(store)
    forward = ForwardAccuracyMonitor(market)
    forward.initialize()
    comp = ShadowModelCompetition(store)
    quotes = FamilyQuoteShadow(store)
    quotes.initialize()
    reference = now-timedelta(minutes=5)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit (
               venue,contract,quote,asset,horizon_minutes,model_version,
               reference_opened_at,reference_close,issued_at,target_opened_at,
               probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
             reference.isoformat(),100.0,now.isoformat(),now.isoformat(),
             .60,.50,100),
        )
        assert comp.issue(
            conn, samples=_samples(now), features=(.3,)*10,
            now=now, venue="hyperliquid", contract="BTC", quote="USDC",
            asset="BTC", horizon=5, reference_opened_at=reference,
            target_opened_at=now, issued_at=now,
            incumbent_up=.60, baseline_up=.50,
        ) == 2
    with store.connect() as conn:
        forecasts = conn.execute(
            "SELECT * FROM rival_model_shadow_forecasts ORDER BY family"
        ).fetchall()
    assert len(forecasts) == 2
    assert all(
        0 < f["probability_up"] < 1
        and f["training_count"] == 140
        and len(f["training_sha256"]) == 64
        and f["issued_at"] == now.isoformat()
        and f["target_opened_at"] == now.isoformat()
        for f in forecasts
    )
    assert comp.settle(now=now+timedelta(minutes=4)) == 0
    assert unattended_acceptance_receipt(
        store,now=now+timedelta(minutes=4),
    )["status"] == "UNVERIFIED"

    enter = now+timedelta(seconds=20)
    leave = now+timedelta(minutes=5,seconds=20)
    assert quotes.observe(_book(enter),now=enter)["opened"] == 2
    assert quotes.observe(_book(enter),now=enter)["opened"] == 0
    assert quotes.observe(_book(leave,bid=101,ask=101.1),now=leave)["closed"] == 2

    observed = now+timedelta(minutes=5,seconds=15)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles (
               venue,contract,quote,interval_minutes,opened_at,closed_at,
               first_observed_at,open_price,high_price,low_price,close_price,
               volume_base,trades)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid","BTC","USDC",5,now.isoformat(),
             (now+timedelta(minutes=5,milliseconds=-1)).isoformat(),
             observed.isoformat(),100.,101.2,99.9,101.,20.,30),
        )
    settled_at = now+timedelta(minutes=6)
    assert forward._settle(settled_at) == 1
    assert comp.settle(now=settled_at) == 2
    report = comp.report(asset="BTC",horizon=5,now=settled_at)
    assert all(
        proof["targets"] == 1
        and proof["brier"] is not None
        and abs(proof["incumbent_brier"]-.16) < 1e-9
        and not proof["live_approved"]
        for proof in report.values()
    )
    receipt = unattended_acceptance_receipt(store,now=settled_at)
    assert receipt["status"] == "HOST_EVIDENCE_PRESENT"
    assert receipt["matched_issued"] == 2
    assert receipt["source_quoted"] == 2
    assert not receipt["model_approved"]
    assert not receipt["restart_replay_verified"]
    return reference, now, settled_at


def test_causal_issue_quote_settlement_comparison_and_restart_is_idempotent(tmp_path):
    path = tmp_path/"restart.sqlite"
    wall = datetime.now(UTC).replace(second=0,microsecond=0)
    now = wall.replace(minute=wall.minute//5*5)-timedelta(minutes=20)
    first = SQLiteHotStore(path)
    try:
        reference, issued, resolved = _run_acceptance(first,now)
    finally:
        first.close()
    # Recreate all production objects and DB connections after a cold restart.
    restarted = SQLiteHotStore(path)
    try:
        forward = ForwardAccuracyMonitor(PerpetualMarketStore(restarted))
        forward.initialize()
        comp = ShadowModelCompetition(restarted)
        assert forward._settle(resolved+timedelta(minutes=1)) == 0
        assert comp.settle(now=resolved+timedelta(minutes=1)) == 0
        with restarted.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit"
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts"
            ).fetchone()["n"] == 2
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_family_quote_shadow"
            ).fetchone()["n"] == 2
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
                "WHERE settled_at IS NOT NULL"
            ).fetchone()["n"] == 2
            assert conn.execute(
                "SELECT reference_opened_at FROM perp_forward_audit"
            ).fetchone()["reference_opened_at"] == reference.isoformat()
        assert unattended_acceptance_receipt(
            restarted,now=resolved+timedelta(minutes=1),
        )["source_quoted"] == 2
        assert FamilyQuoteShadow(restarted).observe(
            _book(issued+timedelta(seconds=20)),
            now=issued+timedelta(seconds=20),
        )["opened"] == 0
    finally:
        restarted.close()


def test_health_incident_transitions_persist_and_do_not_spam(tmp_path):
    store = SQLiteHotStore(tmp_path/"health.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    monitor = RuntimeHealthMonitor(store)
    stamp = monitor.started_at+timedelta(minutes=20)
    try:
        faults = monitor.inspect(
            now=stamp,expected_feeds=(("hyperliquid","BTC"),),
        )
        assert faults["feed:hyperliquid:BTC"] == "never attempted"
        events = monitor.publish(faults,now=stamp)
        assert events == [("OPEN","feed:hyperliquid:BTC","never attempted")]
        assert monitor.publish(faults,now=stamp+timedelta(minutes=1)) == []
        assert len(monitor.snapshot()["issues"]) == 1
        recovered = RuntimeHealthMonitor(store)
        assert len(recovered.snapshot()["issues"]) == 1
        market.record_feed_health(
            venue="hyperliquid", asset="BTC", status="ok",
            attempted_at=stamp+timedelta(minutes=2),
            new_candles=1,total_candles=1,
            last_candle_close=stamp+timedelta(minutes=1),
        )
        clear = monitor.inspect(
            now=stamp+timedelta(minutes=2),
            expected_feeds=(("hyperliquid","BTC"),),
        )
        assert clear == {}
        assert monitor.publish(clear,now=stamp+timedelta(minutes=2)) == [
            ("RESOLVED","feed:hyperliquid:BTC","verified clear on current check")
        ]
        assert monitor.snapshot()["issues"] == []
    finally:
        store.close()


def test_backend_acceptance_and_postgres_connection_pool_reboot(learning_store):
    """CI exercises SQLite and real isolated PostgreSQL, not just a mock cursor."""
    store = learning_store
    wall = datetime.now(UTC).replace(second=0,microsecond=0)
    now = wall.replace(minute=wall.minute//5*5)-timedelta(minutes=20)
    _, _, resolved = _run_acceptance(store,now)
    if store.database.backend == "postgres":
        # Simulate a killed app connection pool while keeping its schema and
        # issued evidence, exactly the restart failure mode we care about.
        dsn = store.database.dsn
        store.close()
        reopened = PostgresHotStore(dsn)
    else:
        reopened = store
    try:
        forward = ForwardAccuracyMonitor(PerpetualMarketStore(reopened))
        forward.initialize()
        assert forward._settle(resolved+timedelta(minutes=1)) == 0
        assert ShadowModelCompetition(reopened).settle(
            now=resolved+timedelta(minutes=1),
        ) == 0
        receipt = unattended_acceptance_receipt(
            reopened,now=resolved+timedelta(minutes=1),
        )
        assert receipt["paired_scored"] == 2
        assert receipt["source_quoted"] == 2
        with reopened.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit"
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_family_quote_shadow"
            ).fetchone()["n"] == 2
    finally:
        if reopened is not store:
            reopened.close()
