"""Matched evidence, fixed tests, no grandfathering, and SQLite/Postgres parity."""
from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from parlay_bot.discord_app.polymarket_watch import PolymarketNotice
from parlay_bot.perpetuals.model_league import (
    ALPHA,
    BOOK,
    FORMULA,
    POLICY_START,
    Contest,
    _p_value,
    _t_tail,
    batch,
    holm,
    league,
    league_text,
    settlement_schema,
)
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch

START = POLICY_START
END = START + timedelta(days=28)
NOW = END + timedelta(days=2, hours=1)
SHA = 'a'*64
CONDITION = '0x' + 'b'*64


def notice(*, start=START, asset='BTC', horizon=60):
    issued = start + timedelta(minutes=horizon*.2)
    return PolymarketNotice(
        asset=asset, horizon=horizon, slug=f'{asset}-{int(start.timestamp())}',
        issued=start, expires=start+timedelta(minutes=horizon), quote_at=issued,
        up_ask=.51, down_ask=.51, up_bid=.49, down_bid=.49,
        condition_id=CONDITION, up_token_id='1'*60, down_token_id='2'*60,
        resolution_source=f'https://data.chain.link/streams/{asset.lower()}-usd-twap-60s-streams',
        rules='Up if the Chainlink 60-second TWAP is at or above the starting price.',
        fee_rate=.07, up_cost=.52749, down_cost=.52749,
    )


def setup(store):
    PolymarketResearch(store).initialize()
    contest = Contest(store)
    contest.initialize(now=START-timedelta(days=1))
    return contest


def seed(store, contest, *, good=True, days=28, missing_model=False):
    rng = random.Random(19)
    for i in range(days*24-1):
        n = notice(start=START+timedelta(hours=i))
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


def test_holm_and_t_tail_match_known_distributions():
    assert holm({'a': .03, 'b': .5}) == set()
    assert holm({'a': .001, 'b': .02}) == {'a', 'b'}
    assert holm({f'm{i}': .0045 for i in range(12)}) == set()
    assert _t_tail(1., 1) == pytest.approx(.25)
    assert _t_tail(2.04523, 29) == pytest.approx(.025, abs=1e-6)
    assert _t_tail(1e-15, 29) == pytest.approx(.5)
    assert _p_value([.1]*28)[2] == 1.  # no zero-variance certainty
    assert _p_value([.1, .2])[2] == 1.  # too few independence units
    assert sum(ALPHA/(i*(i+1)) for i in range(1, 10000)) < ALPHA
    assert batch(START+timedelta(days=29)) is None
    assert batch(NOW)[0] == 1


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
    assert first['alpha'] == ALPHA/2
    # A fresh refresh cannot take another statistical look or change alpha.
    again = league(learning_store, now=NOW+timedelta(minutes=10))
    assert first['markets'] == again['markets'] and first['alpha'] == again['alpha']
    assert all(v['champion'] is None for k, v in first['markets'].items() if k != ('BTC', 60))
    assert entry(learning_store, now=END-timedelta(seconds=1))['champion'] is None


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
                     ((START+timedelta(days=1)).isoformat(), BOOK))
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
