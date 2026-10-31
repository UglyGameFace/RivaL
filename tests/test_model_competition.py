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
    assert comp.settle(now=now+timedelta(minutes=8)) == 2
    assert comp.settle(now=now+timedelta(minutes=8)) == 0
    result = comp.report(asset="BTC",horizon=5,now=now+timedelta(minutes=9))
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
