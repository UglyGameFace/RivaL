"""Matched evidence, fixed tests, no grandfathering, and SQLite/Postgres parity."""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.polymarket_watch import PolymarketNotice
from parlay_bot.perpetuals.model_league import (
    ALPHA,
    BET_FRACTIONS,
    BOOK,
    FORMULA,
    MODELS,
    POLICY,
    Contest,
    _log_capital,
    _test_alpha,
    _update,
    league,
    league_text,
    settlement_schema,
)
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch

START = datetime(2026, 11, 7, tzinfo=UTC)
END = START + timedelta(days=14)
NOW = END + timedelta(minutes=2)
SHA = 'a'*64
CONDITION = '0x' + 'b'*64


def notice(*, start=START, asset='BTC', horizon=60, with_receipt=True):
    issued = start + timedelta(minutes=horizon*.2)
    market = PolymarketNotice(
        asset=asset, horizon=horizon, slug=f'{asset}-{int(start.timestamp())}',
        issued=start, expires=start+timedelta(minutes=horizon), quote_at=issued,
        up_ask=.51, down_ask=.51, up_bid=.49, down_bid=.49,
        condition_id=CONDITION, up_token_id='1'*60, down_token_id='2'*60,
        resolution_source=f'https://data.chain.link/streams/{asset.lower()}-usd-twap-60s-streams',
        rules='Up if the Chainlink 60-second TWAP is at or above the starting price.',
        fee_rate=.07, up_cost=.52749, down_cost=.52749,
    )
    if not with_receipt:
        return market
    source = {
        "slug": market.slug, "condition_id": market.condition_id,
        "up_token_id": market.up_token_id, "down_token_id": market.down_token_id,
        "quote_at": market.quote_at.isoformat(),
        "received_at": market.quote_at.isoformat(),
        "market_version": "v1", "fees_enabled": True,
        "fee_schedule": {"rate": .07, "exponent": 1}, "fee_rate": .07,
        "up_book_sha256": "c" * 64, "down_book_sha256": "d" * 64,
        "shares": 10.0,
    }
    digest = hashlib.sha256(json.dumps(
        source, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    return replace(market, fee_evidence={
        **source, "receipt_sha256": digest,
        "cost_status": "gamma_fee_and_depth_paper_only",
        "actual_fills_proven": False,
    })


def setup(store):
    # Anchor the contest policy at START first. PolymarketResearch.initialize()
    # also initializes the contest, but with a wall-clock anchor, and the policy
    # row is first-write-wins (ON CONFLICT DO NOTHING) — so calling it first
    # would anchor the policy at the test run's wall-clock time and silently
    # exclude every seeded round issued before that instant.
    contest = Contest(store)
    contest.initialize(now=START)
    PolymarketResearch(store).initialize()
    return contest


def seed(store, contest, *, good=True, days=14, missing_model=False,
         horizon=60, with_receipt=True):
    rng = random.Random(19)
    for i in range(days*24*60//horizon-1):
        n = notice(start=START+timedelta(minutes=i*horizon), horizon=horizon,
                   with_receipt=with_receipt)
        p = .9 if rng.random() > .5 else .1
        y = rng.random() < p
        predictions = {BOOK: (p if good else 1-p, SHA), FORMULA: (.5, SHA)}
        if missing_model and i % 2:
            predictions.pop(BOOK)
        assert contest.capture(n, predictions, now=n.quote_at)
        with store.connect() as conn:
            conn.execute(
                'INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)',
                (n.slug, CONDITION, 'UP' if y else 'DOWN', n.expires.isoformat(),
                 (n.expires+timedelta(minutes=1)).isoformat(), SHA, '{}'))


def entry(store, n=None, now=NOW):
    n = n or notice()
    return league(store, now=now)['markets'][n.asset, n.horizon]['scopes'].get(
        settlement_schema(n), {'models': [], 'champion': None})


def test_betting_factors_have_unit_expectation_on_each_conditional_null():
    # Exact one-step expectation, including changing forecast probabilities.
    for p, m in ((.6, .5), (.3, .6), (.99, .01)):
        boundary = (p+m)/2
        g0, g1 = m*m-p*p, (1-m)**2-(1-p)**2
        downside = -min(g0, g1)
        for fraction in BET_FRACTIONS:
            expected = (1-boundary)*(1+fraction*g0/downside)+boundary*(1+fraction*g1/downside)
            assert expected == pytest.approx(1.)
            assert min(1+fraction*g0/downside, 1+fraction*g1/downside) > 0
    for cost in (.1, .5, .95):
        boundary = cost+.01
        for fraction in BET_FRACTIONS:
            expected = (1-boundary)*(1-fraction)+boundary*(1+fraction*(1-boundary)/boundary)
            assert expected == pytest.approx(1.)
    logs = [0.]*len(BET_FRACTIONS)
    assert _log_capital(logs) == 0
    _update(logs, -.1, .1)
    assert _log_capital(logs) < 0  # losses cannot be reset by a new look
    assert _log_capital([10000.]*len(BET_FRACTIONS)) == 10000.


def test_lifetime_budget_covers_all_models_markets_scopes_and_future_artifacts():
    slots = {f'pooled:{i}': i for i in range(1, 101)}
    total = 12*sum(_test_alpha(m, slots, scope)
                   for m in (*MODELS, *slots) for scope in range(1, 101))
    assert 0 < total < ALPHA
    assert _test_alpha('pooled:unknown', slots, 1) == 0
    assert _test_alpha(BOOK, slots, 1) == _test_alpha(FORMULA, slots, 1)


def test_common_snapshot_is_immutable_even_on_restart(learning_store):
    c = setup(learning_store)
    n = notice()
    assert c.capture(n, {BOOK: (.8, SHA)}, now=n.quote_at)
    again = Contest(learning_store)
    assert not again.capture(n, {BOOK: (.1, SHA), FORMULA: (.9, SHA)}, now=n.quote_at+timedelta(seconds=1))
    assert c.call(n.slug, BOOK)['probability_up'] == .8
    assert c.call(n.slug, FORMULA) is None  # no late contestant additions
    with learning_store.connect() as conn:
        row = conn.execute('SELECT * FROM rival_poly_contest_rounds WHERE slug=?', (n.slug,)).fetchone()
    assert row['market_up'] == .5
    assert row['up_cost'] == .52749
    assert row['issued_at'] == n.quote_at.isoformat()


@pytest.mark.parametrize('bad', ['early', 'late', 'stale', 'wrong_source', 'crossed', 'nonfinite'])
def test_bad_receipts_never_enter_the_contest(learning_store, bad):
    c = setup(learning_store)
    n = notice()
    clock = n.quote_at
    forecasts = {BOOK: (.8, SHA)}
    if bad == 'early':
        clock -= timedelta(seconds=1)
    elif bad == 'late':
        clock += timedelta(seconds=21)
    elif bad == 'stale':
        n = replace(n, quote_at=clock-timedelta(seconds=30))
    elif bad == 'wrong_source':
        n = replace(n, resolution_source='https://data.chain.link.evil.invalid/streams/btc-usd')
    elif bad == 'crossed':
        n = replace(n, up_bid=.9)
    else:
        n = replace(n, up_ask=math.nan)
    assert not c.capture(n, forecasts, now=clock)


def test_only_prospective_matched_cost_positive_winner_qualifies(learning_store):
    c = setup(learning_store)
    seed(learning_store, c)
    first = league(learning_store, now=NOW)
    e = entry(learning_store)
    assert e['champion'] == BOOK
    assert next(x for x in e['models'] if x['model'] == BOOK)['coverage'] > .99
    assert first['tests'] >= 12*4  # absent models/markets still count
    assert first['alpha'] == ALPHA
    # A fresh refresh cannot take another statistical look or change alpha.
    again = league(learning_store, now=NOW+timedelta(minutes=10))
    assert first['markets'] == again['markets'] and first['alpha'] == again['alpha']
    assert all(v['champion'] is None for k, v in first['markets'].items() if k != ('BTC', 60))
    assert entry(learning_store, now=START+timedelta(hours=12))['champion'] is None


def test_worse_model_and_incomplete_coverage_never_promote(learning_store):
    c = setup(learning_store)
    seed(learning_store, c, good=False)
    e = entry(learning_store)
    assert e['champion'] is None
    assert 'No model beats Polymarket' in league_text(e, asset='BTC', horizon=60)
    with learning_store.connect() as conn:
        conn.execute('DELETE FROM rival_poly_contest_forecasts WHERE model=?', (BOOK,))
    assert entry(learning_store)['champion'] is None


def test_missing_fees_and_late_enrollment_cannot_qualify(learning_store):
    c = setup(learning_store)
    seed(learning_store, c)
    with learning_store.connect() as conn:
        conn.execute('UPDATE rival_poly_contest_rounds SET up_cost=NULL,down_cost=NULL')
    assert entry(learning_store)['champion'] is None
    with learning_store.connect() as conn:
        conn.execute('UPDATE rival_poly_contest_rounds SET up_cost=.52749,down_cost=.52749')
        conn.execute('UPDATE rival_poly_contest_models SET enrolled_at=? WHERE model=?',
                     ((END).isoformat(), BOOK))
    assert entry(learning_store)['champion'] is None


def test_resolution_cutoff_and_condition_identity_are_enforced(learning_store):
    c = setup(learning_store)
    seed(learning_store, c)
    with learning_store.connect() as conn:
        conn.execute('UPDATE rival_polymarket_ai_outcomes SET observed_at=?',
                     ((END+timedelta(days=3)).isoformat(),))
    assert entry(learning_store)['champion'] is None
    with learning_store.connect() as conn:
        conn.execute('UPDATE rival_polymarket_ai_outcomes SET condition_id=?', ('0x'+'c'*64,))
    assert entry(learning_store)['champion'] is None


def test_formula_only_round_reuses_authoritative_settlement_worker(learning_store):
    c = setup(learning_store)
    n = notice()
    assert c.capture(n, {FORMULA:(.8,SHA)}, now=n.quote_at)
    research = PolymarketResearch(learning_store)
    pending = research._unsettled(n.expires+timedelta(minutes=1))
    assert len(pending) == 1 and pending[0]['slug'] == n.slug
    assert pending[0]['condition_id'] == CONDITION


def test_strong_five_minute_candidate_can_qualify_after_two_days_not_28(learning_store):
    c = setup(learning_store)
    seed(learning_store, c, days=2, horizon=5)
    e = entry(learning_store, notice(horizon=5), now=START+timedelta(days=2))
    assert e['champion'] == BOOK
    best = next(m for m in e['models'] if m['model'] == BOOK)
    assert best['contracts'] == 575  # crosses the 500-round keyset page
    assert best['significant'] and best['trades'] >= 150


def test_missing_early_outcome_blocks_prefix_instead_of_selecting_later_winners(learning_store):
    c = setup(learning_store)
    seed(learning_store, c)
    with learning_store.connect() as conn:
        row = dict(conn.execute('SELECT * FROM rival_polymarket_ai_outcomes WHERE slug=?', (notice().slug,)).fetchone())
        conn.execute('DELETE FROM rival_polymarket_ai_outcomes WHERE slug=?', (notice().slug,))
    e = entry(learning_store)
    assert e['champion'] is None
    assert all(m['contracts'] == 0 and m['blocked'] for m in e['models'])
    with learning_store.connect() as conn:
        conn.execute('INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)', tuple(row.values()))
    assert entry(learning_store)['champion'] == BOOK  # no two-day settlement blackout


def test_new_policy_never_reuses_pre_policy_results(learning_store):
    c = setup(learning_store)
    seed(learning_store, c)
    with learning_store.connect() as conn:
        conn.execute('UPDATE rival_poly_contest_policies SET started_at=? WHERE policy=?', (END.isoformat(), POLICY))
    assert entry(learning_store)['champion'] is None


def test_display_forecast_stays_pinned_across_promotion_and_restart(learning_store):
    c = setup(learning_store)
    n = notice()
    assert c.capture(n, {BOOK: (.1, SHA), FORMULA: (.8, SHA)}, now=n.quote_at)
    first = c.display_call(n.slug, None)
    assert first['model'] == FORMULA and first['probability_up'] == .8
    after = Contest(learning_store).display_call(n.slug, BOOK)
    assert after['model'] == FORMULA and after['probability_up'] == .8


def test_small_sample_reports_progress_without_false_qualification(learning_store):
    c = setup(learning_store)
    seed(learning_store, c, days=1)
    e = entry(learning_store, now=START+timedelta(days=1))
    assert e['champion'] is None
    text = league_text(e, asset='BTC', horizon=60)
    assert '23/300 contracts graded' in text
    assert 'Test calls stay visible' in text and 'alerts start only after' in text



def test_exact_poly_fee_economics_only_uses_frozen_prospective_source(learning_store):
    c = setup(learning_store)
    n = notice()
    source = {
        "slug": n.slug,
        "condition_id": n.condition_id,
        "up_token_id": n.up_token_id,
        "down_token_id": n.down_token_id,
        "quote_at": n.quote_at.isoformat(),
        "received_at": n.quote_at.isoformat(),
        "market_version": "v1",
        "fees_enabled": True,
        "fee_schedule": {"rate": 0.07, "exponent": 1},
        "fee_rate": .07,
        "up_book_sha256": "a"*64,
        "down_book_sha256": "b"*64,
        "shares": 10.0,
    }
    digest = hashlib.sha256(json.dumps(
        source, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    n = replace(n, fee_evidence={
        **source, "receipt_sha256": digest,
        "cost_status": "gamma_fee_and_depth_paper_only",
        "actual_fills_proven": False,
    })
    assert c.capture(n, {BOOK: (.9, SHA)}, now=n.quote_at)
    pending = c.economic_evidence(n.slug, BOOK)
    assert pending["status"] == "source_matched_outcome_pending"
    assert pending["source_matched"] is True
    assert pending["shares"] == 10
    assert pending["executed"] is False
    assert pending["verified_after_cost_edge"] is False
    with learning_store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
            (n.slug, n.condition_id, "UP", n.expires.isoformat(),
             (n.expires+timedelta(minutes=1)).isoformat(), SHA, '{}'),
        )
    scored = c.economic_evidence(n.slug, BOOK)
    assert scored["status"] == "source_matched_paper_outcome"
    assert scored["official_outcome"] == "UP"
    assert scored["side"] == "UP"
    assert scored["net_after_model_fee_and_stress_per_share"] == pytest.approx(
        1-.52749-.01)
    assert scored["promotion_eligible"] is False
    assert scored["verified_after_cost_edge"] is False
    assert not c.capture(
        replace(n, fee_evidence={**n.fee_evidence, "receipt_sha256":"c"*64}),
        {BOOK: (.9, SHA)}, now=n.quote_at,
    )
    # Tampering with stored bytes cannot retroactively alter a cost score.
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_poly_contest_rounds SET evidence_json=? WHERE slug=?",
            ('{}',n.slug),
        )
    assert c.economic_evidence(n.slug, BOOK)["status"] == (
        "stored_market_evidence_changed")


def test_poly_legacy_and_mismatched_fee_receipts_cannot_create_executable_proof(
    learning_store,
):
    c = setup(learning_store)
    n = notice(with_receipt=False)
    assert c.capture(n, {BOOK: (.9, SHA)}, now=n.quote_at)
    assert c.economic_evidence(n.slug, BOOK)["status"] == (
        "missing_issue_time_fee_receipt")
    assert c.economic_evidence("not-issued", BOOK)["status"] == "no_prospective_forecast"


def test_unverified_legacy_economic_numbers_cannot_win_or_issue_a_call(learning_store):
    """A positive statistical backtest is not proof of executable paper cost."""
    c = setup(learning_store)
    seed(learning_store, c, days=14, with_receipt=False)
    result = entry(learning_store)
    assert result["champion"] is None
    record = next(x for x in result["models"] if x["model"] == BOOK)
    assert record["contracts"] == 0
    assert record["observed_contracts"] == 14 * 24 - 1
    assert record["unverified_cost_contracts"] == record["observed_contracts"]
    assert record["source_cost_coverage"] == 0
    assert record["trades"] == 0
    from parlay_bot.perpetuals.learning_health_receipt import _contest_progress

    progress = _contest_progress(learning_store, NOW)
    assert progress["captured"] == record["observed_contracts"]
    assert progress["registered_cost_proofs"] == 0
    assert progress["unverified_cost_contracts"] == progress["captured"]
    assert progress["does_not_prove_executed_trades"] is True
    old = c.call(notice().slug, BOOK)
    assert old["cost_source_verified"] == 0
    assert old["direction"] == "WAIT"
    with learning_store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_poly_contest_cost_proof"
        ).fetchone()["n"] == 0


def test_issued_cost_provenance_rejects_tampered_stored_price(learning_store):
    c = setup(learning_store)
    seed(learning_store, c, days=14)
    original = entry(learning_store)
    assert original["champion"] == BOOK
    original_book = next(x for x in original["models"] if x["model"] == BOOK)
    assert original_book["unverified_cost_contracts"] == 0
    assert original_book["source_cost_coverage"] == 1
    from parlay_bot.perpetuals.learning_health_receipt import _contest_progress

    progress = _contest_progress(learning_store, NOW)
    assert progress["registered_cost_proofs"] == progress["captured"]
    assert c.call(notice().slug, BOOK)["cost_source_verified"] == 1

    # The stored fee-and-ask receipt stays immutable even when someone
    # overwrites the SQL price column directly. No low-price paper advantage.
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_poly_contest_rounds SET up_cost=? WHERE slug=?",
            (.01, notice().slug),
        )
    assert c.call(notice().slug, BOOK)["direction"] == "WAIT"
    assert c.economic_evidence(notice().slug, BOOK)["status"] == (
        "stored_market_evidence_changed")
    changed = entry(learning_store)
    row = next(x for x in changed["models"] if x["model"] == BOOK)
    assert row["contracts"] == original_book["contracts"] - 1
    assert row["unverified_cost_contracts"] == 1
    assert row["source_cost_coverage"] < 1


def test_unmatched_fee_source_hash_or_token_identity_never_enters_paper_score(
    learning_store,
):
    c = setup(learning_store)
    n = notice()
    bad = replace(n, fee_evidence={
        **n.fee_evidence,
        "up_token_id": n.down_token_id,
    })
    assert c.capture(bad, {BOOK: (.9, SHA)}, now=bad.quote_at)
    assert c.call(bad.slug, BOOK)["direction"] == "WAIT"
    assert c.economic_evidence(bad.slug, BOOK)["status"] == (
        "source_fee_or_token_mismatch")
    with learning_store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_poly_contest_cost_proof"
        ).fetchone()["n"] == 0


def test_new_economic_policy_does_not_grandfather_previous_cost_only_rounds(
    learning_store,
):
    c = setup(learning_store)
    seed(learning_store, c, days=14)
    with learning_store.connect() as conn:
        assert conn.execute(
            "SELECT started_at FROM rival_poly_contest_policies WHERE policy=?",
            (POLICY,),
        ).fetchone()["started_at"] == START.isoformat()
        conn.execute(
            "UPDATE rival_poly_contest_policies SET started_at=? WHERE policy=?",
            (END.isoformat(), POLICY),
        )
    assert entry(learning_store)["champion"] is None
