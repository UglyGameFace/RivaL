"""Causal multi-model research, source matching and NO CALL guardrails."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import (
    FAMILIES,
    ShadowModelCompetition,
    shadow_predictions,
)


def _samples(*, now, venue="hyperliquid"):
    examples = []
    for i in range(130):
        issued = now-timedelta(minutes=5*(140-i))
        outcome_known = issued+timedelta(minutes=5)
        features = tuple(float((i+j)%7-3)/3 for j in range(10))
        examples.append(LabeledExample(
            venue=venue, contract="BTC", quote="USDC", horizon_minutes=5,
            issued_at=issued, label_known_at=outcome_known,
            features=features, label_up=int(i % 3 == 0),
        ))
    return examples


def test_shadow_models_train_only_on_known_matching_source_and_are_deterministic():
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    samples = _samples(now=now)
    features = tuple(0.25 for _ in range(10))
    first = shadow_predictions(samples, features, as_of=now)
    assert set(first) == set(FAMILIES)
    assert all(0.02 <= p <= 0.98 for p in first.values())
    # Future labels cannot change predictions made at the original issuance.
    future = replace(samples[-1], issued_at=now+timedelta(minutes=5),
                     label_known_at=now+timedelta(minutes=10), label_up=1)
    assert shadow_predictions(samples+[future], features, as_of=now) == first
    with pytest.raises(ValueError, match="mix instruments"):
        shadow_predictions(samples+[replace(samples[0], venue="okx")],
                           features, as_of=now)
    assert shadow_predictions(samples[:20], features, as_of=now) == {}
    with pytest.raises(ValueError):
        shadow_predictions(samples, (float("nan"),)*10, as_of=now)


def test_model_competition_scores_only_verified_incumbent_future_outcomes(
    learning_store,
):
    # learning_store fixture itself exercises SQLite and isolated PostgreSQL.
    # Keep this test on both backends without creating production tables.
    hot = learning_store
    market = PerpetualMarketStore(hot)
    ForwardAccuracyMonitor(market).initialize()
    comp = ShadowModelCompetition(hot)
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    opened = now-timedelta(minutes=5)
    target = now+timedelta(minutes=5)
    issued = now-timedelta(seconds=20)
    with hot.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit (
               venue,contract,quote,asset,horizon_minutes,model_version,
               reference_opened_at,reference_close,issued_at,target_opened_at,
               probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid","BTC","USDC","BTC",5,MODEL,opened.isoformat(),
             100.,issued.isoformat(),target.isoformat(),.6,.45,100),
        )
        assert comp.issue(
            conn,samples=_samples(now=now),features=(.25,)*10,now=now,
            venue="hyperliquid",contract="BTC",quote="USDC",asset="BTC",
            horizon=5,reference_opened_at=opened,target_opened_at=target,
            issued_at=issued,incumbent_up=.6,baseline_up=.45,
        ) == 2
        # Reissued duplicate must never overwrite its prior probabilities.
        assert comp.issue(
            conn,samples=_samples(now=now),features=(-.25,)*10,now=now,
            venue="hyperliquid",contract="BTC",quote="USDC",asset="BTC",
            horizon=5,reference_opened_at=opened,target_opened_at=target,
            issued_at=issued,incumbent_up=.6,baseline_up=.45,
        ) == 0
    assert comp.settle(now=now+timedelta(minutes=8)) == 0
    with hot.connect() as conn:
        conn.execute(
            """UPDATE perp_forward_audit SET outcome_up=?, settled_at=?,
               model_brier=?,baseline_brier=? WHERE venue=?""",
            (1,(now+timedelta(minutes=6)).isoformat(),.16,.3025,"hyperliquid"),
        )
    # Even a broken legacy incumbent marked settled before the target
    # candle closes must never validate a challenger.
    assert comp.settle(now=now+timedelta(minutes=8)) == 0
    with hot.connect() as conn:
        conn.execute(
            "UPDATE perp_forward_audit SET settled_at=? WHERE venue=?",
            ((now+timedelta(minutes=11)).isoformat(), "hyperliquid"),
        )
    assert comp.settle(now=now+timedelta(minutes=12)) == 2
    assert comp.settle(now=now+timedelta(minutes=12)) == 0
    result = comp.report(asset="BTC",horizon=5,now=now+timedelta(minutes=13))
    assert all(x["targets"] == 1 and not x["qualified_research"]
               and not x["live_approved"] for x in result.values())
    with hot.connect() as conn:
        rows = conn.execute(
            "SELECT probability_up,model_brier,training_sha256,settled_at "
            "FROM rival_model_shadow_forecasts ORDER BY family"
        ).fetchall()
        assert len(rows) == 2
        assert all(x["settled_at"] and x["model_brier"] is not None
                   and len(x["training_sha256"]) == 64 for x in rows)


def test_opposite_exchange_outcomes_cannot_subsidize_bad_model(learning_store):
    """100+ targets on a good venue never prove skill on a bad one."""
    hot = learning_store
    market = PerpetualMarketStore(hot)
    ForwardAccuracyMonitor(market).initialize()
    comp = ShadowModelCompetition(hot)
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    with hot.connect() as conn:
        for day in range(7):
            for index in range(16):
                target = now - timedelta(days=7-day) + timedelta(minutes=5*index)
                issued = target-timedelta(minutes=1)
                settled = target+timedelta(minutes=6)
                for venue,contract,quote,y in (
                    ("hyperliquid","BTC","USDC",1),
                    ("okx","BTC-USDT-SWAP","USDT",0),
                ):
                    p = 0.95
                    for family in FAMILIES:
                        conn.execute(
                            """INSERT INTO rival_model_shadow_forecasts (
                                venue,contract,quote,asset,horizon_minutes,family,
                                reference_opened_at,issued_at,target_opened_at,
                                probability_up,incumbent_up,baseline_up,
                                training_count,training_sha256,
                                settled_at,outcome_up,model_brier,
                                incumbent_brier,baseline_brier)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (venue,contract,quote,"BTC",5,family,
                             (issued-timedelta(minutes=4)).isoformat(),
                             issued.isoformat(),target.isoformat(),p,.5,.5,
                             250,"a"*64,settled.isoformat(),y,
                             (p-y)**2,.25,.25),
                        )
    report = comp.report(asset="BTC",horizon=5,now=now)
    for item in report.values():
        assert item["selected_source"] == "hyperliquid/BTC/USDC"
        assert item["targets"] == 112
        assert item["days"] == 7
        assert item["qualified_research"] is True
        assert item["live_approved"] is False
        assert item["qualified_sources"] == ["hyperliquid/BTC/USDC"]
        assert not item["by_venue"]["okx/BTC-USDT-SWAP/USDT"]["qualified_research"]
        assert item["by_regime"]["unknown"]["high_confidence_misses"] == 112


def test_shadow_batch_preserves_duplicates_and_sqlite_parameter_limit(learning_store):
    """Many candidate rows commit together on SQLite and PostgreSQL."""
    hot = learning_store
    market = PerpetualMarketStore(hot)
    ForwardAccuracyMonitor(market).initialize()
    comp = ShadowModelCompetition(hot)
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    opened = now - timedelta(minutes=5)
    target = now + timedelta(minutes=5)
    issued = now - timedelta(seconds=20)
    pooled = [
        {"family": f"pooled_batch_{i}", "probability": 0.51,
         "training_rows": 100, "model_sha256": f"{i:064x}"}
        for i in range(52)
    ]
    writes = []

    class RecordingConnection:
        def __init__(self, underlying):
            self.underlying = underlying

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO rival_model_shadow_forecasts"):
                writes.append(sql)
            return self.underlying.execute(sql, params)

    with hot.connect() as conn:
        recorded = RecordingConnection(conn)
        arguments = {
            "samples": _samples(now=now), "features": (0.25,) * 10, "now": now,
            "venue": "hyperliquid", "contract": "BTC", "quote": "USDC", "asset": "BTC",
            "horizon": 5, "reference_opened_at": opened, "target_opened_at": target,
            "issued_at": issued, "incumbent_up": 0.6, "baseline_up": 0.45,
            "pooled": pooled,
        }
        assert comp.issue(recorded, **arguments) == 54  # 52 pooled + 2 live
        assert len(writes) == 2  # 50 rows + 4, never 54 roundtrips
        assert comp.issue(recorded, **arguments) == 0
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts"
        ).fetchone()
        assert count["n"] == 54


def _seed_settled(hot, *, start, count, families, outcome=1):
    """Settled incumbent forecasts with unsettled shadow rows on the same target."""
    with hot.connect() as conn:
        for i in range(count):
            reference = start + timedelta(minutes=5 * i)
            target = reference + timedelta(minutes=5)
            issued = reference + timedelta(minutes=5, seconds=20)
            settled = target + timedelta(minutes=6)
            conn.execute(
                """INSERT INTO perp_forward_audit (venue,contract,quote,asset,horizon_minutes,
                     model_version,reference_opened_at,reference_close,issued_at,
                     target_opened_at,probability_up,baseline_up,training_evaluated,
                     settled_at,outcome_up,realized_close,model_brier,baseline_brier)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL, reference.isoformat(),
                 100.0, issued.isoformat(), target.isoformat(), .6, .5, 50,
                 settled.isoformat(), outcome, 100.0 if outcome is None else 101.0,
                 None if outcome is None else (.6 - outcome) ** 2,
                 None if outcome is None else (.5 - outcome) ** 2))
            for family in families:
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                         horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                         probability_up,incumbent_up,baseline_up,training_count,training_sha256)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "BTC", "USDC", "BTC", 5, family, reference.isoformat(),
                     issued.isoformat(), target.isoformat(), .55, .6, .5, 100, "0" * 64))


def _unsettled(hot):
    with hot.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts WHERE settled_at IS NULL"
        ).fetchone()["n"]


def test_settle_grades_more_than_one_batch_per_cycle(learning_store):
    """Several shadow rows per forecast must not outgrow one 240-row batch."""
    from parlay_bot.perpetuals.model_competition import SETTLE_BATCH

    hot = learning_store
    ForwardAccuracyMonitor(PerpetualMarketStore(hot)).initialize()
    comp = ShadowModelCompetition(hot)
    now = datetime(2026, 11, 4, 20, tzinfo=UTC)
    _seed_settled(hot, start=now - timedelta(hours=12), count=100,
                  families=("knn21_v1", "gaussian_nb_v1", "flow_v1"))
    assert 300 > SETTLE_BATCH
    assert comp.settle(now=now, since=now - timedelta(days=3)) == 300
    assert _unsettled(hot) == 0
    assert comp.settle(now=now) == 0


def test_withdrawn_old_targets_stay_unsettled_and_outside_the_fast_pass(learning_store):
    """No-trade incumbents never settle a shadow row; the fast pass skips them."""
    hot = learning_store
    ForwardAccuracyMonitor(PerpetualMarketStore(hot)).initialize()
    comp = ShadowModelCompetition(hot)
    now = datetime(2026, 11, 4, 20, tzinfo=UTC)
    _seed_settled(hot, start=now - timedelta(days=10), count=5,
                  families=("knn21_v1",), outcome=None)
    _seed_settled(hot, start=now - timedelta(hours=2), count=4, families=("knn21_v1",))
    assert comp.settle(now=now, since=now - timedelta(days=3)) == 4
    # The full pass still never grades a withdrawn result.
    assert comp.settle(now=now) == 0
    assert _unsettled(hot) == 5


def test_schema_setup_runs_once_per_store(learning_store):
    hot = learning_store
    ShadowModelCompetition(hot).initialize()
    calls = []
    real = hot.connect

    def counted():
        calls.append(1)
        return real()

    hot.connect = counted
    try:
        ShadowModelCompetition(hot).initialize()
        ShadowModelCompetition(hot).initialize()
    finally:
        hot.connect = real
    assert calls == []
