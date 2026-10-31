"""End-to-end family learning, quote shadow and fail-closed paper promotion."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import FAMILIES


def _seed(store, now, *, quote_count=168, gross=350.0, venue="hyperliquid"):
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    FamilyQuoteShadow(store).initialize()
    family = FAMILIES[0]
    with store.connect() as conn:
        for d in range(21):
            for i in range(8):
                target = now-timedelta(days=23-d)+timedelta(minutes=5*i)
                issued = target-timedelta(seconds=40)
                reference = target-timedelta(minutes=5)
                settled = target+timedelta(minutes=6)
                score = (.95-1.)**2
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (
                      venue,contract,quote,asset,horizon_minutes,family,
                      reference_opened_at,issued_at,target_opened_at,
                      probability_up,incumbent_up,baseline_up,
                      training_count,training_sha256,settled_at,outcome_up,
                      model_brier,incumbent_brier,baseline_brier)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue,"BTC","USDC","BTC",5,family,
                     reference.isoformat(),issued.isoformat(),target.isoformat(),
                     .95,.5,.5,300,"a"*64,settled.isoformat(),1,
                     score,.25,.25),
                )
                if d*8+i >= quote_count:
                    continue
                entry=issued+timedelta(seconds=10)
                exit_at=target+timedelta(minutes=5,seconds=20)
                conn.execute(
                    """INSERT INTO rival_family_quote_shadow (
                      venue,contract,quote,asset,horizon_minutes,family,
                      reference_opened_at,issued_at,target_close_at,
                      training_sha256,challenger_up,incumbent_up,
                      challenger_side,incumbent_side,entry_at,
                      entry_bid,entry_ask,quantity,state,
                      exit_at,exit_bid,exit_ask,
                      challenger_gross_bps,incumbent_gross_bps)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue,"BTC","USDC","BTC",5,family,
                     reference.isoformat(),issued.isoformat(),
                     (target+timedelta(minutes=5)).isoformat(),
                     "a"*64,.95,.5,"LONG","CASH",entry.isoformat(),
                     100.,100.1,.25,"EXIT_QUOTED",
                     exit_at.isoformat(),104.,104.1,gross,0.),
                )
    return family


def test_source_exact_family_paper_promotion_demotes_and_expires(learning_store):
    now=datetime(2026,10,31,16,tzinfo=UTC)
    family=_seed(learning_store,now)
    owner=FamilyPromotion(learning_store)
    kwargs=dict(asset="BTC",venue="hyperliquid",contract="BTC",
                quote="USDC",horizon=5)
    result=owner.evaluate(now=now,**kwargs)
    assert result["selected_family"]==family
    assert result["status"]=="promoted_paper"
    evidence=result["families"][family]
    assert evidence["targets"]==168
    assert evidence["days"]==21
    assert evidence["closed_quotes"]==168
    assert evidence["quote_coverage"]==1.
    assert evidence["calibration_status"]=="research_screen_passed"
    assert evidence["cost_proof"]["severe"]["qualified"]
    assert not result["real_fills_verified"]
    assert owner.selected(now=now,**kwargs)==family
    assert owner.selected(now=now+timedelta(hours=49),**kwargs) is None
    proof=owner.venue_summary(now=now,model_version=family,**kwargs)
    assert proof["model_beats_baseline"] and proof["watch_evidence_passes"]
    assert not proof["real_fills_verified"]
    assert not owner.venue_summary(
        now=now,model_version="knn21_v1",**kwargs,
    )["watch_evidence_passes"]
    assert owner.selected(now=now,**{**kwargs,"venue":"okx"}) is None
    # Same forecast outcomes do not rescue the model when observed books
    # no longer prove favorable after-cost execution.
    with learning_store.connect() as conn:
        conn.execute(
            """UPDATE rival_family_quote_shadow SET challenger_gross_bps=1
               WHERE venue='hyperliquid'""",
        )
    reevaluated=owner.evaluate(now=now,**kwargs)
    assert reevaluated["status"]=="demoted"
    assert owner.selected(now=now,**kwargs) is None


def test_missing_quotes_or_partial_coverage_cannot_promote(learning_store):
    now=datetime(2026,10,31,16,tzinfo=UTC)
    _seed(learning_store,now,quote_count=110)
    owner=FamilyPromotion(learning_store)
    result=owner.evaluate(
        now=now,asset="BTC",venue="hyperliquid",contract="BTC",
        quote="USDC",horizon=5,
    )
    assert result["status"]=="held"
    evidence=result["families"][FAMILIES[0]]
    assert evidence["targets"]==168
    assert evidence["closed_quotes"]==110
    assert evidence["quote_coverage"]<.8
    assert not evidence["qualified"]
    assert not evidence["cost_proof"]["severe"]["qualified"]


def test_quote_observation_prices_incumbent_and_family_on_same_book(learning_store):
    now=datetime(2026,10,31,16,tzinfo=UTC)
    market=PerpetualMarketStore(learning_store)
    ForwardAccuracyMonitor(market).initialize()
    observer=FamilyQuoteShadow(learning_store)
    observer.initialize()
    family=FAMILIES[0]
    issued=now
    target=now+timedelta(minutes=5)
    reference=now
    with learning_store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts (
              venue,contract,quote,asset,horizon_minutes,family,
              reference_opened_at,issued_at,target_opened_at,
              probability_up,incumbent_up,baseline_up,
              training_count,training_sha256)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid","BTC","USDC","BTC",5,family,
             reference.isoformat(),issued.isoformat(),target.isoformat(),
             .85,.5,.5,300,"b"*64),
        )
    entry=now+timedelta(seconds=20)
    book=PerpReferenceBook(
        asset="BTC",venue="hyperliquid",bid=100,ask=100.1,
        bid_quantity=10,ask_quantity=10,
        exchange_at=entry,received_at=entry,
    )
    assert observer.observe(book,now=entry+timedelta(seconds=1))["opened"]==1
    assert observer.observe(book,now=entry+timedelta(seconds=1))["opened"]==0
    close=target+timedelta(minutes=5,seconds=20)
    ending=PerpReferenceBook(
        asset="BTC",venue="hyperliquid",bid=102,ask=102.1,
        bid_quantity=10,ask_quantity=10,
        exchange_at=close,received_at=close,
    )
    assert observer.observe(ending,now=close+timedelta(seconds=1))["closed"]==1
    with learning_store.connect() as conn:
        rows=conn.execute(
            """SELECT state,challenger_side,incumbent_side,
                      challenger_gross_bps,incumbent_gross_bps,
                      training_sha256 FROM rival_family_quote_shadow""",
        ).fetchall()
    assert len(rows)==1
    assert rows[0]["state"]=="EXIT_QUOTED"
    assert rows[0]["challenger_side"]=="LONG"
    assert rows[0]["incumbent_side"]=="CASH"
    assert rows[0]["challenger_gross_bps"]>0
    assert rows[0]["incumbent_gross_bps"]==0
    assert rows[0]["training_sha256"]=="b"*64
