"""Threshold outcome settler (sweep fix 2026-11-24).

The G-C estate candidates were NOT_MEASURABLE because nothing
recorded how threshold markets settled. The settler must resolve
expired markets from the venues' own records, leave indecisive or
unfinal markets unresolved (never guessed), and never re-check a
market it already resolved.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.perpetuals import threshold_outcomes as outcomes
from parlay_bot.perpetuals.kalshi_thresholds import KalshiThresholdsRecorder
from parlay_bot.perpetuals.threshold_markets import ThresholdMarketsRecorder
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
PAST = (NOW - timedelta(days=1)).isoformat()
FUTURE = (NOW + timedelta(days=1)).isoformat()


class _Client:
    """Canned venue answers keyed by URL fragment."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(url)
        haystack = url + " " + str(params or {})
        request = httpx.Request("GET", url)
        for fragment, payload in self.answers.items():
            if fragment in haystack:
                return httpx.Response(200, json=payload, request=request)
        return httpx.Response(404, json={}, request=request)


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "outcomes.sqlite")
    ThresholdMarketsRecorder(store).initialize()
    KalshiThresholdsRecorder(store).initialize()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_threshold_market_snapshots (snapshot_at,"
            " asset, contract_type, horizon, series_id, series_slug,"
            " condition_id, strike_low, end_date) VALUES"
            " (?, 'BTC', 'above', 'weekly', 1, 'btc-weekly', 'cond-yes',"
            " 90000.0, ?)", ("2026-11-27T00:00:00+00:00", PAST))
        conn.execute(
            "INSERT INTO rival_threshold_market_snapshots (snapshot_at,"
            " asset, contract_type, horizon, series_id, series_slug,"
            " condition_id, strike_low, end_date) VALUES"
            " (?, 'BTC', 'above', 'weekly', 1, 'btc-weekly', 'cond-split',"
            " 95000.0, ?)", ("2026-11-27T00:00:00+00:00", PAST))
        conn.execute(
            "INSERT INTO rival_threshold_market_snapshots (snapshot_at,"
            " asset, contract_type, horizon, series_id, series_slug,"
            " condition_id, strike_low, end_date) VALUES"
            " (?, 'BTC', 'above', 'weekly', 1, 'btc-weekly', 'cond-open',"
            " 99000.0, ?)", ("2026-11-27T00:00:00+00:00", FUTURE))
        conn.execute(
            "INSERT INTO rival_kalshi_threshold_snapshots (snapshot_at,"
            " asset, contract_type, horizon, series_ticker, market_ticker,"
            " strike_low, close_time) VALUES"
            " (?, 'BTC', 'above', 'weekly', 'KXBTCW', 'KXBTCW-1',"
            " 90000.0, ?)", ("2026-11-27T00:00:00+00:00", PAST))
    return store


def _answers():
    return {
        "cond-yes": [{"closed": True, "outcomePrices": '["1", "0"]'}],
        "cond-split": [{"closed": True, "outcomePrices": '["0.5", "0.5"]'}],
        "KXBTCW-1": {"market": {"result": "no", "status": "finalized"}},
    }


def test_settler_records_decisive_outcomes_only(tmp_path):
    store = _store(tmp_path)
    settler = outcomes.ThresholdOutcomeSettler(store)
    client = _Client(_answers())
    receipt = asyncio.run(settler.settle(client, now=NOW))
    assert receipt["polymarket"] == {"due": 2, "checked": 2, "resolved": 1}
    assert receipt["kalshi"] == {"due": 1, "checked": 1, "resolved": 1}
    assert outcomes.settled_count(store) == 2
    with store.connect() as conn:
        rows = {r[1]: r for r in conn.execute(
            "SELECT venue, market_key, resolved_yes, strike_low"
            " FROM rival_threshold_outcomes", ()).fetchall()}
    assert rows["cond-yes"][2] == 1
    assert rows["cond-yes"][3] == 90000.0
    assert rows["cond-split"][2] is None  # split market: never guessed
    assert rows["KXBTCW-1"][2] == 0
    assert "cond-open" not in rows  # not yet expired: untouched


def test_settler_never_rechecks_resolved_markets(tmp_path):
    store = _store(tmp_path)
    settler = outcomes.ThresholdOutcomeSettler(store)
    asyncio.run(settler.settle(_Client(_answers()), now=NOW))
    client = _Client(_answers())
    receipt = asyncio.run(settler.settle(client, now=NOW))
    assert receipt["polymarket"]["due"] == 1  # only the split market
    assert receipt["kalshi"]["due"] == 0
    assert outcomes.settled_count(store) == 2


class _GammaClient:
    """Emulates Gamma's real listing behaviour: closed markets are
    omitted from the default /markets listing and only appear when
    the request asks for them (closed=true)."""

    def __init__(self):
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(dict(params or {}))
        request = httpx.Request("GET", url)
        if (params or {}).get("closed") == "true":
            return httpx.Response(
                200, json=[{"closed": True,
                            "outcomePrices": '["1", "0"]'}],
                request=request)
        return httpx.Response(200, json=[], request=request)


def test_settler_asks_gamma_for_closed_markets(tmp_path):
    # Regression (live-verified 2026-11-25): without closed=true the
    # Gamma fallback could never resolve any market, so Polymarket
    # outcomes older than the settlement index's coverage stalled at
    # the head of the due queue forever.
    store = _store(tmp_path)
    settler = outcomes.ThresholdOutcomeSettler(store)
    client = _GammaClient()
    receipt = asyncio.run(settler.settle(client, now=NOW))
    assert receipt["polymarket"]["resolved"] == 2
    assert any(c.get("closed") == "true" for c in client.calls)
    with store.connect() as conn:
        rows = {r[1]: r for r in conn.execute(
            "SELECT venue, market_key, resolved_yes"
            " FROM rival_threshold_outcomes", ()).fetchall()}
    assert rows["cond-yes"][2] == 1
    assert rows["cond-split"][2] == 1


def test_settled_count_is_zero_without_the_table(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    assert outcomes.settled_count(store) == 0


class _DictCursor:
    """Cursor proxy returning rows as dicts keyed by output column
    names (aliases included) -- the Postgres dict_row shape."""

    def __init__(self, cursor):
        self._cursor = cursor

    def _names(self):
        return [d[0] for d in self._cursor.description or ()]

    def fetchall(self):
        names = self._names()
        return [dict(zip(names, row)) for row in self._cursor.fetchall()]

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else dict(zip(self._names(), row))


class _DictConn:
    def __init__(self, manager):
        self._manager = manager
        self._conn = None

    def __enter__(self):
        self._conn = self._manager.__enter__()
        return self

    def __exit__(self, *exc):
        return self._manager.__exit__(*exc)

    def execute(self, sql, params=()):
        return _DictCursor(self._conn.execute(sql, params))


class _DictStore:
    """Store proxy whose rows arrive as dicts, as on Postgres.

    Regression cover for the production failure in which _due read
    item["key"]/item["expiry"] from dict rows keyed by the raw column
    names (condition_id / end_date): every settle cycle raised
    KeyError, the bot logged and swallowed it, and zero outcomes were
    ever recorded. The SQLite tests above cannot see that shape --
    sqlite3.Row is not a dict, so the positional branch runs.
    """

    def __init__(self, inner):
        self._inner = inner
        self.database = inner.database

    def connect(self):
        return _DictConn(self._inner.connect())


def test_settler_finds_markets_buried_beyond_a_flat_scan_window(tmp_path):
    """An expired market stays discoverable no matter how many newer
    snapshot rows other markets pile on top of its last one -- the
    flat latest-N-rows scan lost exactly those markets (every market
    that expired while the settler was down)."""
    store = _store(tmp_path)
    base = datetime(2026, 11, 27, 1, 0, tzinfo=UTC)
    with store.connect() as conn:
        for i in range(4100):
            conn.execute(
                "INSERT INTO rival_threshold_market_snapshots"
                " (snapshot_at, asset, contract_type, horizon, series_id,"
                " series_slug, condition_id, strike_low, end_date) VALUES"
                " (?, 'BTC', 'above', 'weekly', 1, 'btc-weekly',"
                " 'cond-open', 99000.0, ?)",
                ((base + timedelta(minutes=i)).isoformat(), FUTURE))
    settler = outcomes.ThresholdOutcomeSettler(store)
    receipt = asyncio.run(settler.settle(_Client(_answers()), now=NOW))
    assert receipt["polymarket"]["due"] == 2  # cond-yes + cond-split
    assert outcomes.settled_count(store) == 2


def test_settler_records_outcomes_with_dict_rows(tmp_path):
    store = _DictStore(_store(tmp_path))
    settler = outcomes.ThresholdOutcomeSettler(store)
    receipt = asyncio.run(settler.settle(_Client(_answers()), now=NOW))
    assert receipt["polymarket"] == {"due": 2, "checked": 2, "resolved": 1}
    assert receipt["kalshi"] == {"due": 1, "checked": 1, "resolved": 1}
    assert outcomes.settled_count(store) == 2
