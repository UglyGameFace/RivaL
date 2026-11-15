"""One model's history must not crowd later models out of source-exact proof."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import family_governance
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.proof_progress import proof_progress
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet, model_sha256
from tests.test_family_cost_promotion import _seed
from tests.xmarket_fixtures import valid_doc


def _rows(conn, family, targets, sha):
    rows = []
    for i, target in enumerate(targets):
        outcome = i % 2
        rows.append((
            "hyperliquid", "BTC", "USDC", "BTC", 15, family,
            (target-timedelta(minutes=15)).isoformat(),
            (target-timedelta(minutes=9,seconds=30)).isoformat(), target.isoformat(),
            .6,.5,.5,200,sha,(target+timedelta(minutes=6)).isoformat(),outcome,
            (.6-outcome)**2,.25,.25,
        ))
    # Stay below SQLite's portable 999-parameter bound on both backends.
    for offset in range(0,len(rows),40):
        batch = rows[offset:offset+40]
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts
               (venue,contract,quote,asset,horizon_minutes,family,
                reference_opened_at,issued_at,target_opened_at,probability_up,
                incumbent_up,baseline_up,training_count,training_sha256,
                settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
               VALUES """ + ",".join("("+",".join("?" for _ in range(19))+")" for _ in batch),
            tuple(value for row in batch for value in row),
        )


def test_later_xmarket_family_survives_other_families_filling_the_global_cap(learning_store):
    now = datetime.now(UTC).replace(minute=0,second=0,microsecond=0)
    ShadowModelCompetition(learning_store).initialize()
    FamilyQuoteShadow(learning_store).initialize()
    models = XmarketModelSet(learning_store)
    document = valid_doc(venues=(), asset="BTC", horizon=15, now=now)
    assert models.import_document("test-model",json.dumps(document).encode()) == 1
    sha = model_sha256(document)
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_xmarket_models SET imported_at=?",
                     ((now-timedelta(hours=3)).isoformat(),))
        targets = [now-timedelta(days=28)+timedelta(minutes=5*i) for i in range(8000)]
        # Three genuine per-artifact family names; each fits in 30 calendar days.
        for digit in ("1","2","3"):
            _rows(conn,"boosted_logistic_"+digit*12,targets,digit*64)
        _rows(conn,"xmarket_v1",[now-timedelta(hours=2),now-timedelta(minutes=105)],sha)
    result = FamilyPromotion(learning_store).evaluate(
        asset="BTC",venue="hyperliquid",contract="BTC",quote="USDC",horizon=15,now=now,
    )
    assert "xmarket_v1" in result["families"]
    xmarket = result["families"]["xmarket_v1"]
    assert xmarket["targets"] == 2
    assert xmarket["invalid_rows"] == 0
    assert not xmarket["qualified"]


def test_incomplete_family_input_cannot_promote_or_appear_as_complete_proof(
    learning_store,monkeypatch,
):
    now = datetime(2026,11,2,16,tzinfo=UTC)
    family = _seed(learning_store,now)
    monkeypatch.setattr(family_governance,"MAX_FORECASTS_PER_FAMILY",167)
    result = FamilyPromotion(learning_store).evaluate(
        asset="BTC",venue="hyperliquid",contract="BTC",quote="USDC",horizon=5,now=now,
    )
    partial = result["families"][family]
    assert partial["input_rows"] == 167
    assert not partial["input_complete"]
    assert partial["invalid_rows"] == 0
    assert not partial["qualified"]
    assert result["status"] == "held"
    read = proof_progress(learning_store,asset="BTC",venue="hyperliquid",now=now)
    assert not read["evaluations"][0]["paper_qualified"]
    assert read["evaluations"][0]["candidate"]["reason"] == "Evaluation input is incomplete"


def test_other_artifact_rows_do_not_consume_the_active_pins_input_budget(
    learning_store,monkeypatch,
):
    now = datetime.now(UTC).replace(minute=0,second=0,microsecond=0)
    ShadowModelCompetition(learning_store).initialize()
    FamilyQuoteShadow(learning_store).initialize()
    document = valid_doc(venues=(),asset="BTC",horizon=15,now=now)
    XmarketModelSet(learning_store).import_document("test-pin",json.dumps(document).encode())
    with learning_store.connect() as conn:
        _rows(conn,"xmarket_v1",[now-timedelta(hours=3,minutes=15*i) for i in range(6)],"b"*64)
        _rows(conn,"xmarket_v1",[now-timedelta(hours=2),now-timedelta(minutes=105)],model_sha256(document))
    monkeypatch.setattr(family_governance,"MAX_FORECASTS_PER_FAMILY",3)
    result = FamilyPromotion(learning_store).evaluate(
        asset="BTC",venue="hyperliquid",contract="BTC",quote="USDC",horizon=15,now=now,
    )
    evidence = result["families"]["xmarket_v1"]
    assert evidence["input_complete"]
    assert evidence["input_rows"] == evidence["targets"] == 2
    assert evidence["invalid_rows"] == 0
    assert not evidence["qualified"]
