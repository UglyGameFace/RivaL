"""Production failure scenarios: restart, contention, missing input and recovery."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import httpx
import pytest

from parlay_bot.discord_app.error_log import ProblemGroup
from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.discord_app.status_card import (
    StatusFacts,
    assess,
    build_embed,
    problem_lines,
    problem_state,
)
from parlay_bot.discord_app.status_publisher import StatusPublisher
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.research_hypotheses import list_blocked_receipts, record_blocked_receipt
from parlay_bot.research.source_runs import record_source_run, source_run_receipt
from parlay_bot.research.training_outcomes import summarize
from parlay_bot.storage.diagnostics import QueryDiagnostics, collect_and_record, recurring_faults
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore
from parlay_bot.storage.read_cache import SharedReads

NOW = datetime(2026, 11, 12, 5, 20, tzinfo=UTC)
MONTHS = [f'2026-{m:02d}' for m in range(5, 11)]


def test_six_minute_card_cannot_claim_startup_warnings_stopped():
    boot = NOW - timedelta(minutes=6)
    group = ProblemGroup('parlay_bot.storage.database', 'WARNING', 415,
                         NOW - timedelta(hours=12), NOW - timedelta(seconds=15),
                         'RivaL PostgreSQL slow operation owner=forward_audit.summary:20',
                         boot_count=2, boot_first_at=NOW - timedelta(minutes=2))
    assert problem_state(group, now=NOW, boot_at=boot) == 'active'
    facts = StatusFacts(now=NOW, boot_at=boot, ready=True, incidents=[], problems=[group])
    assert assess(facts)[0] == 'degraded'
    text = '\n'.join(problem_lines([group], now=NOW, boot_at=boot, totals={}))
    assert '415× in 24 h; 2 this boot' in text
    assert 'recovery unverified' in text
    assert 'only right after' not in text


def test_research_progress_is_information_but_stalled_watchdog_is_a_fault():
    facts = StatusFacts(now=NOW, boot_at=NOW - timedelta(hours=1), ready=True,
                        incidents=[], research=[('shadow-progress:demo', '📊 testing'),
                                               ('research-loop:counts', '🔬 2 discarded')])
    assert assess(facts) == ('healthy', ())
    facts.watchdog_expected = True
    facts.watchdog_checked_at = (NOW - timedelta(minutes=8)).isoformat()
    assert 'watchdog:stalled' in assess(facts)[1]
    facts.watchdog_checked_at = (NOW + timedelta(minutes=1)).isoformat()
    assert 'watchdog:stalled' in assess(facts)[1]


class Channel:
    id = 123

    def __init__(self):
        self.sent = 0
        self.edited = []
        self.failure = None

    async def send(self, **kwargs):
        self.sent += 1
        return SimpleNamespace(id=900 + self.sent)

    def get_partial_message(self, message_id):
        channel = self

        class Message:
            async def edit(self, **kwargs):
                if channel.failure:
                    raise channel.failure
                channel.edited.append(message_id)

        return Message()


@pytest.mark.asyncio
async def test_status_refresh_reuses_message_across_process_restarts(tmp_path):
    store = SQLiteHotStore(tmp_path / 'status.sqlite')
    channel = Channel()
    embed = discord.Embed(title='RivaL Status')
    try:
        await StatusPublisher(store).refresh(channel, embed, now=NOW)
        await StatusPublisher(store).refresh(channel, embed, now=NOW + timedelta(minutes=2))
        assert channel.sent == 1 and channel.edited == [901]
        channel.failure = discord.Forbidden(SimpleNamespace(status=403, reason='forbidden'),
                                             {'code': 50013, 'message': 'Missing permissions'})
        with pytest.raises(discord.Forbidden):
            await StatusPublisher(store).refresh(channel, embed, now=NOW)
        assert channel.sent == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_status_identity_save_does_not_duplicate_the_message(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / 'identity.sqlite')
    publisher, channel = StatusPublisher(store), Channel()
    original = publisher._save
    attempts = []

    def flaky(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('temporarily unavailable')
        original(*args)

    monkeypatch.setattr(publisher, '_save', flaky)
    try:
        with pytest.raises(OSError):
            await publisher.refresh(channel, discord.Embed(), now=NOW)
        await publisher.refresh(channel, discord.Embed(), now=NOW)
        assert channel.sent == 1 and channel.edited == [901]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_only_deleted_status_message_is_recreated(tmp_path):
    store = SQLiteHotStore(tmp_path / 'deleted.sqlite')
    channel, publisher = Channel(), StatusPublisher(store)
    try:
        await publisher.refresh(channel, discord.Embed(), now=NOW)
        channel.failure = discord.NotFound(SimpleNamespace(status=404, reason='not found'),
                                           {'code': 10003, 'message': 'Unknown Channel'})
        with pytest.raises(discord.NotFound):
            await publisher.refresh(channel, discord.Embed(), now=NOW)
        assert channel.sent == 1
        channel.failure = discord.NotFound(SimpleNamespace(status=404, reason='not found'),
                                           {'code': 10008, 'message': 'Unknown Message'})
        await publisher.refresh(channel, discord.Embed(), now=NOW)
        assert channel.sent == 2
        channel.failure = None
        await StatusPublisher(store).refresh(channel, discord.Embed(), now=NOW)
        assert channel.sent == 2 and channel.edited[-1] == 902
    finally:
        store.close()


def test_large_status_remains_deliverable_with_all_three_sections():
    problems = [ProblemGroup(f'worker{i}', 'WARNING', 500,
                            NOW-timedelta(hours=1), NOW-timedelta(seconds=1),
                            f'Worker {i}: ' + 'slow query ' * 100) for i in range(30)]
    facts = StatusFacts(now=NOW, boot_at=NOW-timedelta(hours=2), ready=True,
                        incidents=[{'issue_key': f'fault:{i}', 'first_seen_at': NOW.isoformat(),
                                    'detail': 'pending diagnostics ' * 100} for i in range(30)],
                        research=[(f'research-blocked:{i}', '⛔ missing source ' * 100)
                                  for i in range(30)], problems=problems)
    embed, _, _ = build_embed(facts)
    assert len(embed) <= 6000 and len(embed.fields) <= 25
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert any(field.name.startswith('Checks') for field in embed.fields)
    assert any(field.name.startswith('Predictions') for field in embed.fields)
    assert any(field.name.startswith('Errors') for field in embed.fields)


def test_cold_read_burst_is_computed_once_and_failures_are_not_cached():
    cache, started, release = SharedReads(), threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {'values': [10]}

    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(cache.read, ('summary', 'BTC'), 300, load)
        assert started.wait(1)
        others = [pool.submit(cache.read, ('summary', 'BTC'), 300, load) for _ in range(4)]
        for _ in range(100):
            if cache.snapshot()['coalesced_reads'] == 4:
                break
            threading.Event().wait(.005)
        release.set()
        results = [future.result(2) for future in [first, *others]]
    assert len(calls) == 1 and cache.snapshot()['coalesced_reads'] == 4
    results[0]['values'].append(20)
    assert results[1]['values'] == [10]
    with pytest.raises(ValueError):
        cache.read(('bad',), 300, lambda: (_ for _ in ()).throw(ValueError('bad read')))
    assert cache.read(('bad',), 300, lambda: 42) == 42


@pytest.mark.asyncio
async def test_btc_then_xrp_waits_for_real_capacity_and_persists_both(tmp_path, monkeypatch):
    from parlay_bot.perpetuals import local_first_research as module

    class Clock(datetime):
        value = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.value

    sleeps = []

    async def wait(seconds):
        sleeps.append(seconds)
        Clock.value += timedelta(seconds=seconds)

    def evidence(_market, *, asset, **kwargs):
        return ({'asset': asset, 'sources': [{'venue': v} for v in ('okx', 'gate', 'bitget')],
                 'analysis': 'unvalidated_research', 'horizon_minutes': 5},
                ('a' if asset == 'BTC' else 'b') * 64)

    def response(_request):
        return httpx.Response(200, json={
            'model': module.LOCAL_MODEL, 'direction': 'uncertain', 'confidence': 'low',
            'thesis': 'Insufficient future evidence', 'risk': 'No verified edge',
            'uncalibrated': True, 'informational_only': True})

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module.asyncio, 'sleep', wait)
    monkeypatch.setattr(module, 'build_evidence', evidence)
    store = SQLiteHotStore(tmp_path / 'cpu.sqlite')
    market = PerpetualMarketStore(store)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        ai = module.LocalFirstResearch(market, local_key='test', client=client)
        try:
            btc = await ai.ask(asset='BTC', venues=('okx', 'gate', 'bitget'), busy_retry=True)
            xrp = await ai.ask(asset='XRP', venues=('okx', 'gate', 'bitget'), busy_retry=True)
            assert sleeps and sleeps[0] >= 60
            for result in (btc, xrp):
                assert market.cached_ai_research(asset=result.asset, model=result.model,
                                                 evidence_sha256=result.evidence_sha256)
        finally:
            store.close()


def test_unmetered_private_calls_keep_spacing_and_explicit_caps(tmp_path):
    store = SQLiteHotStore(tmp_path / 'quota.sqlite')
    market = PerpetualMarketStore(store)
    try:
        for i in range(25):
            assert market.reserve_ai_request(model='local', now=NOW + timedelta(seconds=61*i),
                                             daily_max=0, cooldown_seconds=60)
        assert not market.reserve_ai_request(model='local', now=NOW + timedelta(seconds=61*24+1),
                                             daily_max=0, cooldown_seconds=60)
        for i in range(2):
            assert market.reserve_ai_request(model='capped', now=NOW + timedelta(seconds=61*i),
                                             daily_max=2, cooldown_seconds=60)
        assert not market.reserve_ai_request(model='capped', now=NOW + timedelta(minutes=5),
                                             daily_max=2, cooldown_seconds=60)
    finally:
        store.close()


def complete_source(months=MONTHS):
    return {'venue': 'gate', 'months': months, 'status': 'verified_research_archived_no_qualified_models',
            'source_scopes': [{'contract': f'{a}_USDT', 'quote': 'USDT', 'interval': 5}
                              for a in ('BTC', 'ETH', 'SOL', 'XRP')]}


def test_verified_source_reconciles_same_period_but_not_a_different_period(tmp_path):
    store = SQLiteHotStore(tmp_path / 'source.sqlite')
    try:
        record_blocked_receipt(store, venue='gate', asset=None, requested_months=6,
                               missing_months=['2026-06'], report_sha256='a'*64, now=NOW)
        old = source_run_receipt(complete_source([f'2025-{m:02d}' for m in range(5, 11)]))
        record_source_run(store, receipt=old, report_sha256='b'*64, now=NOW + timedelta(minutes=1))
        assert len(list_blocked_receipts(store)) == 1
        fresh = source_run_receipt(complete_source())
        assert fresh['status'] == 'verified'  # rejected models do not invalidate verified sources
        assert record_source_run(store, receipt=fresh, report_sha256='c'*64,
                                 now=NOW + timedelta(minutes=2)) == 1
        assert record_source_run(store, receipt=fresh, report_sha256='c'*64,
                                 now=NOW + timedelta(minutes=3)) == 0
        assert list_blocked_receipts(store) == []
        assert 'research-blocked:gate' not in dict(research_alerts(store, now=NOW+timedelta(minutes=4)))
    finally:
        store.close()


def test_no_scopes_means_all_requested_months_unverified():
    receipt = source_run_receipt({'venue': 'hyperliquid', 'requested_months': MONTHS,
                                  'status': 'source_months_incomplete', 'source_scopes': []})
    assert receipt['reason'] == 'no_source_scopes_restored'
    assert receipt['missing_months'] == MONTHS and receipt['status'] == 'incomplete'


def test_source_receipt_rejects_false_recovery_and_invalid_months(tmp_path):
    with pytest.raises(ValueError, match='six requested months'):
        source_run_receipt(complete_source(['2026-13'] * 6))
    store = SQLiteHotStore(tmp_path / 'invalid.sqlite')
    try:
        receipt = source_run_receipt(complete_source())
        receipt['qualified_scopes'] = 0
        with pytest.raises(ValueError, match='cannot clear'):
            record_source_run(store, receipt=receipt, report_sha256='e'*64, now=NOW)
    finally:
        store.close()


def test_recurring_sql_records_are_bounded_and_do_not_expose_private_values():
    diagnostics = QueryDiagnostics()
    for _ in range(3):
        diagnostics.observe(owner='forward_audit.summary:22', label='forecast#abcdef12',
                            sql='SELECT ? AS private_value', params=('secret-not-for-logs',),
                            seconds=2.5, error=None)
    report = diagnostics.snapshot()
    assert 'secret-not-for-logs' not in json.dumps(report)
    assert 'SELECT' not in json.dumps(report)
    assert recurring_faults(report, now=NOW, boot_at=NOW-timedelta(minutes=6)) == {}
    assert recurring_faults(report, now=NOW, boot_at=NOW-timedelta(hours=1))


def test_deadlock_graph_keeps_sqlstate_and_owners_without_server_sql():
    error = RuntimeError('private exception message')
    error.sqlstate = '40P01'
    error.diag = SimpleNamespace(message_detail=(
        'Process 101 waits for ShareLock on transaction 303; blocked by process 202.\n'
        'Process 202 waits for ShareLock on transaction 304; blocked by process 101.\n'
        "Process 101: UPDATE private_table SET token='secret-binding'"))
    diagnostics = QueryDiagnostics(lambda: [
        {'pid': 101, 'owner': 'forward.issue:55', 'operation': 'UPDATE'},
        {'pid': 202, 'owner': 'collector.save:66', 'operation': 'UPDATE'}])
    evidence = diagnostics.error_evidence(error)
    diagnostics.observe(owner='forward.issue:55', label='forward#12345678',
                        sql='UPDATE private_table SET token=?', params=('secret-binding',),
                        seconds=.5, error='DeadlockDetected', error_evidence=evidence)
    group = diagnostics.snapshot()['groups'][0]
    assert group['sqlstates'] == {'40P01': 1}
    graph = group['lock_evidence'][0]
    assert {(e['pid'], e['blocked_by_pid']) for e in graph['deadlock_edges']} == {
        (101, 202), (202, 101)}
    assert graph['pool_owners_at_error'][1]['owner'] == 'collector.save:66'
    assert diagnostics.snapshot()['latest_lock_failures_this_boot'][0]['sqlstate'] == '40P01'
    encoded = json.dumps(group)
    assert all(value not in encoded for value in ('secret-binding', 'private_table',
                                                  'private exception', 'token='))


@pytest.mark.skipif(not os.getenv('RIVAL_TEST_DATABASE_URL'), reason='PostgreSQL integration')
def test_postgres_plan_sampling_is_read_only_and_redacts_predicates():
    store = PostgresHotStore(os.environ['RIVAL_TEST_DATABASE_URL'])
    try:
        store.database.diagnostics.observe(owner='test.read', label='none#abcdef12',
            sql='SELECT ?::text AS private_value', params=('private-binding',), seconds=3, error=None)
        report = collect_and_record(store)
        assert report['plan']['status'] == 'captured_without_execution'
        assert 'private-binding' not in json.dumps(report)
        assert 'private_value' not in json.dumps(report)
    finally:
        store.close()


def test_training_execution_and_model_acceptance_are_separate(tmp_path):
    for family in ('archive_v1', 'gbm_v1', 'volcal_v1'):
        for horizon in (5, 15, 60):
            (tmp_path / f'{family}-{horizon}-report.json').write_text(json.dumps({
                'family': family, 'asset': 'BTC', 'horizon_minutes': horizon,
                'training_status': 'rejected', 'gate_failures': ['test_brier_gain<=0.005']}))
    result = summarize(tmp_path, asset='BTC')
    assert result['execution_complete'] and result['accepted'] == 0 and result['rejected'] == 9
    (tmp_path / 'volcal_v1-60-report.json').unlink()
    assert not summarize(tmp_path, asset='BTC')['execution_complete']


@pytest.mark.asyncio
async def test_silent_connected_book_is_reconnected_and_can_recover(monkeypatch):
    from parlay_bot.perpetuals import stream_quotes as module

    sessions, recovered = [], asyncio.Event()

    class Socket:
        async def __aenter__(self):
            sessions.append(self)
            self.number = len(sessions)
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _data):
            return None

        async def recv(self):
            await asyncio.sleep(.001)
            if self.number == 1:
                return '{"channel":"subscriptionResponse"}'
            recovered.set()
            return json.dumps({'channel': 'l2Book', 'data': {
                'coin': 'BTC', 'time': int(datetime.now(UTC).timestamp()*1000),
                'levels': [[{'px': '100', 'sz': '2', 'n': 1}],
                           [{'px': '101', 'sz': '2', 'n': 1}]]}})

    monkeypatch.setattr(module.websockets, 'connect', lambda *args, **kwargs: Socket())
    monkeypatch.setattr(module, 'BOOK_SILENCE_SECONDS', .01)
    monkeypatch.setattr(module, 'RECEIVE_CHECK_SECONDS', .005)
    monkeypatch.setattr(module, 'RECONNECT_CAP_SECONDS', .001)
    monkeypatch.setattr(module.random, 'uniform', lambda *args: 0)
    hub = module.StreamingBookHub(assets=('BTC',))
    task = asyncio.create_task(hub._run('hyperliquid'))
    try:
        await asyncio.wait_for(recovered.wait(), 1)
        for _ in range(50):
            if hub.health()['hyperliquid']['updates']:
                break
            await asyncio.sleep(.001)
        health = hub.health()['hyperliquid']
        assert health['silent_reconnects'] >= 1 and health['updates'] >= 1
        assert not health['blocked']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unexpected_training_error_saves_report_and_finishes_other_families(tmp_path, monkeypatch):
    from parlay_bot.research import weekly_archive as module

    def fit(trainer, frames, training):
        if trainer.FAMILY == 'gbm_v1':
            raise ValueError('synthetic broken feature shape')
        return {'gates_passed': False, 'gate_failures': ['no edge']}, None, 'rejected'

    monkeypatch.setattr(module, '_fit', fit)
    result = await module.train_weekly_archive({}, training={
        'asset': 'BTC', 'horizon': 5, 'trained_through': NOW.isoformat()},
        census={}, out=tmp_path, publish=False)
    assert result == 1
    reports = [json.loads(path.read_text()) for path in tmp_path.glob('*report.json')]
    assert len(reports) == 3
    failed = next(r for r in reports if r['family'] == 'gbm_v1')
    assert failed['training_status'] == 'error'
    assert failed['gate_failures'] == ['training_exception:ValueError']
    assert failed['trace']
