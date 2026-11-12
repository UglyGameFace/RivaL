"""Bounded, private SQL diagnostics for recurring production failures.

SQL and bindings exist only in a small in-memory plan queue. Persisted records
contain fingerprints, timings, wait kinds and structural EXPLAIN nodes; never
SQL, parameters, filter expressions, DSNs or provider messages. EXPLAIN does
not execute the captured statement. Sampling runs beside the watchdog.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, OrderedDict, deque
from datetime import UTC, datetime, timedelta

from parlay_bot.storage.query_scope import statement_timeout

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_runtime_diagnostics (
    component TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload_json TEXT NOT NULL)"""
WINDOW = 600
MAX_GROUPS = 32


def structural_plan(plan: dict) -> list[dict]:
    nodes, pending = [], [plan]
    allowed = ('Node Type', 'Relation Name', 'Index Name', 'Plan Rows', 'Total Cost')
    while pending and len(nodes) < 40:
        node = pending.pop(0)
        nodes.append({k: node[k] for k in allowed if k in node})
        pending.extend(node.get('Plans', []))
    return nodes


class QueryDiagnostics:
    def __init__(self, peer_snapshot=None) -> None:
        self.lock = threading.Lock()
        self.events: OrderedDict[tuple, deque] = OrderedDict()
        self.plans: OrderedDict[tuple, tuple] = OrderedDict()
        self.last_plan: dict[tuple, float] = {}
        self.peer_snapshot = peer_snapshot
        self.lock_failures: deque = deque(maxlen=8)

    def error_evidence(self, exc: Exception) -> dict:
        """Retain SQLSTATE and lock edges, never the server's statement prose."""
        state = getattr(exc, 'sqlstate', None)
        evidence = {'sqlstate': state} if isinstance(state, str) and re.fullmatch(
            r'[A-Z0-9]{5}', state) else {}
        if state == '40P01':
            detail = str(getattr(getattr(exc, 'diag', None), 'message_detail', '') or '')[:16000]
            edges = re.findall(
                r'Process (\d+) waits for ([A-Za-z]+) on (transaction|relation) (\d+); '
                r'blocked by process (\d+)', detail, flags=re.IGNORECASE)
            evidence['deadlock_edges'] = [
                {'pid': int(pid), 'mode': mode, 'object_kind': kind,
                 'object_id': int(identity), 'blocked_by_pid': int(blocker)}
                for pid, mode, kind, identity, blocker in edges[:12]]
            if self.peer_snapshot is not None:
                evidence['pool_owners_at_error'] = self.peer_snapshot()
        return evidence

    def observe(self, *, owner: str, label: str, sql: str, params,
                seconds: float, error: str | None, error_evidence: dict | None = None) -> None:
        if 'diagnostics.' in owner or (seconds < 2 and error is None):
            return
        now = time.monotonic()
        key = (owner.split(':', 1)[0], label)
        with self.lock:
            if (error_evidence or {}).get('sqlstate') == '40P01':
                self.lock_failures.append({
                    'observed_at': datetime.now(UTC).isoformat(), 'owner': key[0],
                    'statement': label, **error_evidence})
            events = self.events.pop(key, deque(maxlen=128))
            events.append((now, seconds, error, error_evidence or {}))
            self.events[key] = events
            if (sql.lstrip().upper().startswith(('SELECT ', 'WITH '))
                    and now - self.last_plan.get(key, -WINDOW) >= WINDOW):
                self.plans[key] = (sql, tuple(params))
            while len(self.events) > MAX_GROUPS:
                old, _ = self.events.popitem(last=False)
                self.plans.pop(old, None)
                self.last_plan.pop(old, None)
            while len(self.plans) > MAX_GROUPS:
                self.plans.popitem(last=False)

    def snapshot(self) -> dict:
        now = time.monotonic()
        groups = []
        with self.lock:
            for (owner, label), events in self.events.items():
                recent = [event for event in events if now - event[0] <= WINDOW]
                if recent:
                    groups.append({'owner': owner, 'statement': label,
                                   'slow_count': sum(e[1] >= 2 for e in recent),
                                   'error_count': sum(e[2] is not None for e in recent),
                                   'error_classes': dict(Counter(e[2] for e in recent if e[2])),
                                   'sqlstates': dict(Counter(e[3]['sqlstate'] for e in recent
                                                            if e[3].get('sqlstate'))),
                                   'lock_evidence': [e[3] for e in recent
                                                     if e[3].get('sqlstate') == '40P01'][-8:],
                                   'max_seconds': round(max(e[1] for e in recent), 3),
                                   'last_age_seconds': round(now - recent[-1][0], 1)})
            lock_failures = list(self.lock_failures)
        return {'window_seconds': WINDOW, 'groups': groups,
                'latest_lock_failures_this_boot': lock_failures}

    def collect(self, store) -> dict:
        snapshot = self.snapshot()
        snapshot['checked_at'] = datetime.now(UTC).isoformat()
        database = store.database
        with database._lock:
            borrowers = list(database._borrowers.values())
            pids = [getattr(getattr(b._raw, 'info', None), 'backend_pid', None)
                    for _, _, b in borrowers]
            pids = [p for p in pids if p is not None]
            snapshot['pool'] = {'checked_out': len(borrowers), 'maximum': database._max_size}
        if not snapshot['groups']:
            return snapshot
        with self.lock:
            item = self.plans.popitem(last=False) if self.plans else None
            if item:
                self.last_plan[item[0]] = time.monotonic()
        try:
            with statement_timeout(1500), store.connect() as conn:
                conn.execute('SET TRANSACTION READ ONLY')
                if pids:
                    rows = conn.execute(
                        'SELECT state,wait_event_type,COUNT(*) AS n FROM pg_stat_activity '
                        f"WHERE pid IN ({','.join('?' for _ in pids)}) "
                        'GROUP BY state,wait_event_type', tuple(pids)).fetchall()
                    snapshot['waits'] = [dict(row) for row in rows]
                if item:
                    (owner, label), (sql, params) = item
                    row = conn.execute('EXPLAIN (FORMAT JSON) ' + sql, params).fetchone()
                    document = row['QUERY PLAN']
                    if isinstance(document, str):
                        document = json.loads(document)
                    snapshot['plan'] = {'owner': owner, 'statement': label,
                                        'status': 'captured_without_execution',
                                        'nodes': structural_plan(document[0]['Plan'])}
        except Exception as exc:  # noqa: BLE001 - diagnostics never interrupt ingestion
            snapshot['sampling_error_class'] = type(exc).__name__
        return snapshot


def collect_and_record(store) -> dict:
    diagnostics = getattr(store.database, 'diagnostics', None)
    if diagnostics is None:
        return {'supported': False, 'groups': []}
    report = diagnostics.collect(store)
    from parlay_bot.storage.read_cache import shared_reads

    report['shared_reads'] = shared_reads(store).snapshot()
    with statement_timeout(2000), store.connect() as conn:
        conn.execute(SCHEMA)
        conn.execute('''INSERT INTO rival_runtime_diagnostics VALUES (?,?,?)
            ON CONFLICT(component) DO UPDATE SET checked_at=excluded.checked_at,
            payload_json=excluded.payload_json''',
                     ('database', report['checked_at'], json.dumps(report, allow_nan=False)))
    return report


def recurring_faults(report: dict, *, now: datetime, boot_at: datetime) -> dict[str, str]:
    if now - boot_at < timedelta(minutes=10):
        return {}
    faults = {}
    for group in report.get('groups', []):
        if group['slow_count'] >= 3 or group['error_count'] >= 3:
            faults[f"database:{group['owner']}:{group['statement']}"] = (
                f"{group['slow_count']} slow operations / {group['error_count']} failures "
                f"in 10 min, max {group['max_seconds']}s; structural diagnostics recorded; "
                'recovery unverified')
    return faults


def record_component(store, component: str, payload: dict, *, now: datetime) -> None:
    """Small durable outcome record used by existing workers and host receipts."""
    with store.connect() as conn:
        conn.execute(SCHEMA)
        conn.execute('''INSERT INTO rival_runtime_diagnostics VALUES (?,?,?)
            ON CONFLICT(component) DO UPDATE SET checked_at=excluded.checked_at,
            payload_json=excluded.payload_json''',
                     (component, now.isoformat(), json.dumps(payload, allow_nan=False)))


def record_model_diagnostics(store, scores, stops, *, now: datetime) -> None:
    """Reuse the watchdog aggregate; never re-scan the growing forecast table."""
    from parlay_bot.perpetuals.early_stop import verdict

    weak = [dict(row) for row in scores if int(row.get('n', 0)) >= 50
            and (float(row['model_score']) > float(row['incumbent_score']) + .025
                 or float(row['incumbent_score']) > .25)]
    weak.sort(key=lambda r: float(r['model_score']) - float(r['incumbent_score']), reverse=True)
    scopes = []
    for row in weak[:24]:
        scopes.append({key: row.get(key) for key in
                       ('venue', 'contract', 'quote', 'horizon_minutes', 'family',
                        'n', 'days', 'model_score', 'incumbent_score', 'loss_square')})
        scopes[-1]['flat_probability_brier'] = .25
        scopes[-1]['early_stop_eligible'] = verdict(row) is not None
        scopes[-1]['evidence_type'] = 'raw_scored_rows; not independent trades or promotion proof'
    record_component(store, 'model_evaluation', {
        'status': 'recorded', 'weak_scopes': scopes, 'new_stops': len(stops),
        'automatic_action': 'existing per-scope early stop; promotion gates retained',
        'remaining': 'future matched outcomes and validated feature/calibration comparisons',
    }, now=now)
