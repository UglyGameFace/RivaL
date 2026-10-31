"""Persistent causal learning, source isolation and real forward selection gates."""
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.historical_challengers import HistoricalChallengers
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_checkpoints import LiveModelCheckpoints, canonical
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner, book_features
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
from parlay_bot.research.live_training import evaluate_compatible
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore
from tests.test_live_model_checkpoints import samples
from tests.test_polymarket_contract_research import notice
from tests.test_research_candidate_registry import artifact


def compatible_artifact(*, gain=.02):
    source = json.loads(artifact())
    spec = source['model_spec']
    from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT, TARGET_CONTRACT
    spec.update(model='offline-live-contract-logistic-v3', weights=[.1]*11,
                feature_contract=FEATURE_CONTRACT, target_contract=TARGET_CONTRACT,
                method=f'fixture-{gain}', training_count=500, training_cutoff='2026-05-01T00:00:00+00:00',
                validation_cutoff='2026-07-01T00:00:00+00:00',
                evaluation_completed_through='2026-09-30T23:59:59+00:00')
    for key in ('validation','out_of_sample_test'):
        source[key].update(brier_gain=gain, days=30)
    source['model_sha256'] = hashlib.sha256(canonical(spec).encode()).hexdigest()
    return json.dumps(source).encode()


def test_online_memory_survives_rolling_eviction_and_learns_once(tmp_path):
    hot = SQLiteHotStore(tmp_path/'rolling.sqlite')
    now = datetime.now(UTC)
    original = samples(now)
    learner = LiveModelCheckpoints(hot)
    prior = learner.fit(original, as_of=now)
    latest = replace(original[-1], issued_at=original[-1].issued_at+timedelta(minutes=5),
                     label_known_at=original[-1].label_known_at+timedelta(minutes=5))
    restarted = LiveModelCheckpoints(hot)
    learned = restarted.fit((*original[30:], latest), as_of=now+timedelta(minutes=5))
    assert learned.updates == 81  # Eviction does not erase the first 30 updates.
    assert learned.training_labels == 81
    assert learned.historical_training_labels == 0
    assert learned.historical_seed_sha256 is None
    assert learned.final_weights != prior.final_weights
    assert restarted.fit((*original[30:], latest), as_of=now+timedelta(minutes=6)) == learned
    hot.close()


def test_only_exact_screened_past_artifacts_can_seed_research(tmp_path):
    hot = SQLiteHotStore(tmp_path/'seed.sqlite')
    now = datetime.now(UTC)
    registry = ResearchCandidateRegistry(hot)
    registry.import_verified('legacy', artifact())
    registry.import_verified('failed', compatible_artifact(gain=-.01))
    scope = {'venue': 'binance', 'contract': 'BTCUSDT', 'quote': 'USDT', 'horizon': 5, 'now': now}
    assert registry.compatible_seed(**scope) is None
    registry.import_verified('passed', compatible_artifact(), now=now)
    candidate = registry.compatible_seed(**scope)
    assert candidate is not None
    assert registry.compatible_seed(**{**scope,'venue':'bitget'}) is None
    assert registry.compatible_seed(**{**scope,'now':datetime(2026,9,1,tzinfo=UTC)}) is None
    rows = tuple(replace(s, venue='binance', contract='BTCUSDT', quote='USDT') for s in samples(now))
    state = LiveModelCheckpoints(hot).fit(rows, as_of=now, seed=candidate)
    restored = LiveModelCheckpoints(hot).fit(rows, as_of=now+timedelta(seconds=1), seed=candidate)
    assert state == restored
    assert LiveModelCheckpoints(hot).summary()['historical_seeded_checkpoints'] == 1
    assert state.historical_seed_sha256 == candidate["model_sha256"]
    assert state.historical_training_labels == candidate["training_count"]
    assert state.training_labels == state.historical_training_labels + state.updates
    assert registry.summary(asset='BTC',horizon=5)['active_offline_model'] is False
    hot.close()


@pytest.mark.parametrize('blocker', [None, 'missing_baseline', 'strong_baseline',
                                     'pre_import', 'late_issue', 'single_day'])
def test_research_challenger_needs_actual_paired_forward_proof_and_rolls_back(learning_store, blocker):
    hot = learning_store
    PerpetualMarketStore(hot).initialize()
    now = datetime.now(UTC).replace(second=0,microsecond=0)
    registry = ResearchCandidateRegistry(hot)
    registry.import_verified('passed',compatible_artifact(),
                             now=now-timedelta(days=10) if blocker != 'pre_import' else now)
    candidate = registry.compatible_seed(venue='binance',contract='BTCUSDT',quote='USDT',horizon=5,now=now)
    rows = tuple(replace(s,venue='binance',contract='BTCUSDT',quote='USDT') for s in samples(now))
    states = LiveModelCheckpoints(hot)
    incumbent = states.fit(rows,as_of=now)
    monitor = HistoricalChallengers(hot, states)
    from types import SimpleNamespace
    last = SimpleNamespace(venue='binance',contract='BTCUSDT',quote='USDT',
                           opened_at=datetime.fromtimestamp(int(now.timestamp())//300*300,UTC),close_price=100.)
    assert monitor.choose(rows,rows[-1].features,last,horizon=5,now=now,incumbent=incumbent) == incumbent
    with hot.connect() as conn:
        for day in range(1,9):
            start = (now-timedelta(days=day if blocker != 'single_day' else 1)).replace(hour=0,minute=0)
            if blocker == 'single_day':
                start += timedelta(minutes=day*200)
            for index in range(20):
                target = start+timedelta(minutes=index*10)
                conn.execute('''INSERT INTO rival_history_forward
                    (model_sha256,venue,contract,quote,horizon_minutes,reference_opened_at,reference_close,
                     issued_at,target_opened_at,probability_up,incumbent_up,weights_sha256,outcome_up,settled_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (candidate['model_sha256'],'binance','BTCUSDT','USDT',5,
                     (target-timedelta(minutes=5)).isoformat(),100.,
                     (target+timedelta(minutes=6) if blocker == 'late_issue' else target-timedelta(minutes=4)).isoformat(),
                     target.isoformat(),.9,.5,'a'*64,1,(target+timedelta(minutes=5)).isoformat()))
                if blocker != 'missing_baseline':
                    conn.execute('INSERT INTO rival_history_forward_baselines VALUES (?,?,?,?,?,?,?,?)',
                                 (candidate['model_sha256'],'binance','BTCUSDT','USDT',5,
                                  (target-timedelta(minutes=5)).isoformat(),
                                  (target+timedelta(minutes=6) if blocker == 'late_issue' else target-timedelta(minutes=4)).isoformat(),
                                  .99 if blocker == 'strong_baseline' else .5))
    monitor.choose(rows,rows[-1].features,last,horizon=5,now=now,incumbent=incumbent)
    with hot.connect() as conn:
        selected = conn.execute('SELECT * FROM rival_history_selection').fetchone()
        assert selected['selected_sha256'] == (candidate['model_sha256'] if blocker is None else None)
        conn.execute('UPDATE rival_history_forward SET probability_up=.1 WHERE outcome_up IS NOT NULL')
    assert monitor.choose(rows,rows[-1].features,last,horizon=5,now=now,incumbent=incumbent) == incumbent
    with hot.connect() as conn:
        assert conn.execute('SELECT selected_sha256 FROM rival_history_selection').fetchone()['selected_sha256'] is None
    hot.close()


@pytest.mark.parametrize('backend',['sqlite','postgres'])
def test_polymarket_learns_only_receipts_reuses_weights_and_freezes_forward_forecast(tmp_path,backend):
    if backend == 'postgres' and not os.getenv('RIVAL_TEST_DATABASE_URL'):
        pytest.skip('PostgreSQL CI service required')
    hot = PostgresHotStore(os.environ['RIVAL_TEST_DATABASE_URL']) if backend == 'postgres' else SQLiteHotStore(tmp_path/'poly.sqlite')
    service = PolymarketResearch(hot)
    service.initialize()
    tables = ['rival_polymarket_observations','rival_polymarket_ai_outcomes',
              'rival_polymarket_model_forward','rival_polymarket_model_state']
    with hot.connect() as conn:
        for table in tables:
            conn.execute(f'DELETE FROM {table}')
    current = notice()
    now = datetime.now(UTC)
    features = canonical(book_features(current))
    try:
        for i in range(100):
            close = now-timedelta(minutes=10+i*5)
            slug, condition = f'learning-test-{i}',f'0x{i+1:064x}'
            with hot.connect() as conn:
                conn.execute('''INSERT INTO rival_polymarket_observations
                    (slug,condition_id,asset,horizon,resolution_source,expires_at,observed_at,features_json,evidence_sha256)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (slug,condition,current.asset,current.horizon,current.resolution_source,
                     close.isoformat(),(close-timedelta(minutes=4)).isoformat(),features,'b'*64))
            receipt = {'condition_id': condition,'status': 'resolved','extended_review': False,
                           'transaction_hash': '0x'+'c'*64,'resolved_block': 1000+i,
                           'resolved_at': (close+timedelta(seconds=10)).isoformat(),'payouts': [1000000,0]}
            assert service._record_resolution({'slug': slug,'condition_id': condition,'expires_at': close.isoformat()},receipt,now=now) == 1
        result = service.learner.observe(current)
        assert result['training_labels'] == 100
        assert result['direction'] == 'UP'
        with hot.connect() as conn:
            first = dict(conn.execute('SELECT * FROM rival_polymarket_model_forward WHERE slug=?',(current.slug,)).fetchone())
        restarted = PolymarketOutcomeLearner(hot)
        result = restarted.observe(replace(current, up_ask=.7,down_ask=.31,quote_at=datetime.now(UTC)))
        assert result['training_labels'] == 100
        assert restarted.summary()['learned_contracts'] == 100
        with hot.connect() as conn:
            same = dict(conn.execute('SELECT * FROM rival_polymarket_model_forward WHERE slug=?',(current.slug,)).fetchone())
        assert first == same  # A refresh is never another independent outcome.
        assert restarted.summary()['verified_trading_edge'] is False
    finally:
        with hot.connect() as conn:
            for table in tables:
                conn.execute(f'DELETE FROM {table}')
        hot.close()


def test_final_holdout_does_not_select_or_fit_historical_weights():
    start = datetime(2026,1,1,tzinfo=UTC)
    identity = {'venue': 'binance','contract': 'BTCUSDT','quote': 'USDT','interval_minutes': 5}
    rows = [{'opened_at': (start+timedelta(minutes=5*i)).isoformat(),
                 'closed_at': (start+timedelta(minutes=5*i+5,milliseconds=-1)).isoformat(),
                 'close': 100.+(i % 7)} for i in range(1800)]
    vectors = {i:tuple([.1*(i % 3)]*10) for i in range(39,1800)}
    provenance = {'source_sha256_by_month':{'2026-01':'a'*64,'2026-02':'b'*64},'months':['2026-01','2026-02']}
    first = evaluate_compatible([],horizon=5,prepared=(identity,rows,provenance,vectors))
    altered = [dict(r) for r in rows]
    for i in range(1550,len(altered)):
        altered[i]['close'] = 100.+i
    second = evaluate_compatible([],horizon=5,prepared=(identity,altered,provenance,vectors))
    assert first['model_spec']['weights'] == second['model_spec']['weights']
    assert first['model_spec']['method'] == second['model_spec']['method']
    assert first['out_of_sample_test'] != second['out_of_sample_test']


def test_prespecified_historical_model_family_keeps_live_feature_contract():
    from parlay_bot.research.live_training import _fit
    examples = [
        (datetime(2026, 1, 1, tzinfo=UTC),
         datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5),
         tuple(float((i+j)%5)/10 for j in range(10)), i % 2)
        for i in range(150)
    ]
    samples = [(a, b, tuple(c), y) for a,b,c,y in examples]
    sparse, _ = _fit(samples, l2=.005, rate=.01, active=(0,1,5,6))
    slow, _ = _fit(samples, l2=.005, rate=.006, epochs=3)
    assert len(sparse) == len(slow) == 11
    assert all(sparse[i+1] == 0 for i in set(range(10)) - {0,1,5,6})
    assert slow != sparse
    with pytest.raises(ValueError):
        _fit(samples, l2=.005, active=())


def test_seed_diagnosis_distinguishes_missing_rejected_and_source_mismatch(tmp_path):
    hot = SQLiteHotStore(tmp_path/'diagnosis.sqlite')
    registry = ResearchCandidateRegistry(hot)
    scope = {'asset': 'BTC', 'horizon': 5, 'live_venues': ('hyperliquid',)}
    assert registry.summary(**scope)['slot_imported'] == 0
    registry.import_verified('legacy', artifact())
    assert registry.summary(**scope)['slot_compatible'] == 0
    registry.import_verified('failed', compatible_artifact(gain=-.01))
    assert 'failed' in registry.summary(**scope)['seed_reason']
    registry.import_verified('passed', compatible_artifact())
    report = registry.summary(**scope)
    assert report['slot_passed'] == 1
    assert report['slot_live_source_matched'] == 0
    assert 'no configured live' in report['seed_reason']
    assert 'future' in registry.summary(asset='BTC', horizon=5, live_venues=('binance',))['seed_reason']
    hot.close()
