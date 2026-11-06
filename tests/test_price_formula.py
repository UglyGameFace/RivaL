"""Numerical references, causal inputs and common checkpoint integration."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from unittest.mock import Mock

import pytest

from parlay_bot.perpetuals.model_league import BOOK, FORMULA, PRESSURE
from parlay_bot.perpetuals.polymarket_formula_test import FormulaTest
from parlay_bot.perpetuals.price_formula import (
    chance_up,
    jump_chance,
    jump_parameters,
    microprice,
    per_second_sigma,
)
from parlay_bot.perpetuals.spot_reference import VENUES, clean_candles, combine
from tests.test_model_league import SHA, notice, setup


def test_digital_gbm_uses_d2_and_rejects_bad_inputs():
    sigma, seconds = .0002, 100.
    price = 100 * math.exp(2*sigma*math.sqrt(seconds)+.5*sigma*sigma*seconds)
    assert chance_up(price, 100, sigma, seconds) == pytest.approx(.977249868)
    assert chance_up(100, 100, sigma, seconds) < .5
    for value in (math.nan, math.inf, -1, 0):
        assert chance_up(value, 100, sigma, seconds) is None
        assert chance_up(100, 100, value, seconds) is None
    assert per_second_sigma([100.]*30) is None
    assert per_second_sigma([100.]*20+[math.nan, 101.]) is None


def test_ewma_reacts_more_to_a_recent_shock():
    quiet = [100*math.exp(.0001*(i % 2)) for i in range(120)]
    recent = quiet+[quiet[-1]*1.02]
    old = [quiet[0]/1.02]+quiet
    assert per_second_sigma(recent) > 5*per_second_sigma(old)


def test_twap_digital_differs_from_terminal_and_requires_unaccrued_window():
    # Moment variance tends to sigma^2*(T-2w/3); verify independent formula.
    s, k, sigma, t, w = 100.1, 100., .0002, 240., 60
    with localcontext() as ctx:
        ctx.prec = 50
        x = Decimal(str(sigma))**2*w
        ratio = 2*(x.exp()-1-x)/(x*x)
        v = float(Decimal(str(sigma))**2*Decimal(str(t-w))+ratio.ln())
    expected = .5*math.erfc(-(math.log(s/k)-v/2)/math.sqrt(2*v))
    assert chance_up(s, k, sigma, t, twap_seconds=w) == pytest.approx(expected, abs=1e-8)
    assert chance_up(s, k, sigma, t, twap_seconds=w) != chance_up(s, k, sigma, t)
    assert chance_up(s, k, sigma, 30, twap_seconds=w) is None


def test_merton_zero_jump_limit_and_tail_estimation():
    p = {'sigma': .0002, 'intensity': 0., 'mean': 0., 'variance': .01}
    assert jump_chance(100.1, 100., 240., p) == pytest.approx(chance_up(100.1, 100., .0002, 240.))
    assert jump_parameters([100.]*240) is None
    closes = [100*math.exp(.0002*math.sin(i)) for i in range(240)]
    closes[80] *= 1.02
    closes[160] *= .98
    fitted = jump_parameters(closes)
    assert fitted is not None and fitted['intensity'] > 0
    assert 0 <= jump_chance(100.1, 100., 240., fitted) <= 1
    assert microprice(99., 101., 9., 1.) == 100.8
    assert microprice(101., 99., 9., 1.) is None


def reference(now, opens):
    base = int(opens.timestamp())-240*60
    rows = [(base+i*60, 100., 100*math.exp(.0002*math.sin(i))) for i in range(240)]
    rows += [(int(opens.timestamp()), 100., 100.01)]
    return {'price': 100.2, 'open': 100., 'closes': [c for _, _, c in rows],
            'microprice': 100.21, 'at': now, 'sources': {v: {'price': 100.2} for v in VENUES},
            'candles': rows}


def test_median_requires_three_usd_sources_and_consecutive_closed_candles():
    n = notice(horizon=5)
    now = n.quote_at
    ref = reference(now, n.issued)
    rows = clean_candles(ref['candles'], now=now)
    assert rows is not None
    assert clean_candles(rows[:-2], now=now) is None
    assert clean_candles(rows[:100]+rows[101:], now=now) is None
    sources = {v: {'price': 100.+i*.1, 'microprice': 100.+i*.1,
                    'at': now, 'clock': 'book', 'candles': rows}
               for i, v in enumerate(VENUES[:3])}
    combined = combine(sources, opens=n.issued, now=now)
    assert combined['price'] == 100.1 and combined['open'] == 100.
    sources.pop('gemini')
    assert combine(sources, opens=n.issued, now=now) is None


def test_every_model_and_wait_share_one_frozen_book(learning_store):
    setup(learning_store)
    n = notice(horizon=5)
    learner = Mock()
    learner.contest_forecasts.return_value = {BOOK: (.8, SHA)}
    test = FormulaTest(learning_store)
    reading = test.consider(n, reference=reference(n.quote_at, n.issued), learner=learner, now=n.quote_at)
    assert reading['strike_source'] == 'three-spot-median-proxy'
    original = test.contest.call(n.slug, FORMULA)
    assert original is not None and test.contest.call(n.slug, PRESSURE) is not None
    assert test.contest.call(n.slug, BOOK)['issued_at'] == original['issued_at']
    assert test.contest.call(n.slug, BOOK)['market_up'] == original['market_up']
    # Later, even an attractive quote and probability cannot rewrite the call.
    test.consider(replace(n, up_ask=.1), reference=None, learner=learner,
                  now=n.quote_at+timedelta(seconds=1))
    assert test.contest.call(n.slug, FORMULA) == original
    with learning_store.connect() as conn:
        raw = conn.execute('SELECT evidence_json,evidence_sha256 FROM rival_poly_contest_rounds').fetchone()
    assert hashlib.sha256(raw['evidence_json'].encode()).hexdigest() == raw['evidence_sha256']
    assert json.loads(raw['evidence_json'])['inputs']['candle_count'] > 120


def test_published_strike_overrides_exchange_proxy_and_unknown_rules_abstain(learning_store):
    setup(learning_store)
    n = replace(notice(horizon=5), price_to_beat=101.)
    learner = Mock()
    learner.contest_forecasts.return_value = {}
    test = FormulaTest(learning_store)
    reading = test.consider(n, reference=reference(n.quote_at, n.issued), learner=learner, now=n.quote_at)
    assert reading['strike_source'] == 'published' and reading['chance_up'] < .5
    other = replace(n, slug='unknown-rules', rules='Unknown settlement')
    assert test.consider(other, reference=None, learner=learner, now=n.quote_at) is None
    assert test.contest.call('unknown-rules', FORMULA) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('venue', VENUES)
async def test_public_source_wire_formats_and_cooldown(venue):
    from datetime import UTC, datetime
    from email.utils import format_datetime

    import httpx

    from parlay_bot.perpetuals.spot_reference import SpotReference

    now = datetime.now(UTC)
    minute = int(now.timestamp())//60*60
    rows = [(minute-i*60, 100., 100.+.01*(i % 2)) for i in range(250)]
    requests = []
    blocked = False

    def handler(request):
        requests.append(request)
        if blocked:
            return httpx.Response(429)
        if 'candles' in request.url.path or 'ohlc' in request.url.path:
            if venue == 'coinbase':
                payload = [[t, o, o+.1, o, c, 1.] for t, o, c in rows]
            elif venue == 'bitstamp':
                payload = {'data': {'ohlc': [{'timestamp': str(t), 'open':str(o), 'close':str(c)} for t,o,c in rows]}}
            elif venue == 'gemini':
                payload = [[t*1000, o, o+.1, o, c, 1.] for t,o,c in rows]
            else:
                payload = [[t*1000, o, c, o+.1, o, 1.] for t,o,c in rows]
        elif venue == 'coinbase':
            payload = {'time': now.isoformat(), 'bids':[['99.99','3','1']], 'asks':[['100.01','1','1']]}
        elif venue == 'bitstamp':
            payload = {'timestamp': str(int(now.timestamp())), 'bids':[['99.99','3']], 'asks':[['100.01','1']]}
        elif venue == 'bitfinex':
            payload = [[99.99, 1, 3.], [100.01, 1, -1.]]
        else:
            # Documented dummy timestamp is deliberately old, never trusted.
            payload = {'bids':[{'price':'99.99','amount':'3','timestamp':'1'}],
                       'asks':[{'price':'100.01','amount':'1','timestamp':'1'}]}
        return httpx.Response(200, json=payload, headers={'Date': format_datetime(now)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SpotReference(client)
        result = await source._source(venue, 'BTC')
        assert result is not None and result['price'] == 100.
        assert len(result['candles']) == 240 and result['microprice'] > result['price']
        blocked = True
        assert await source._source(venue, 'BTC') is None
        count = len(requests)
        assert await source._source(venue, 'BTC') is None
        assert len(requests) == count  # blocked provider is not hammered
