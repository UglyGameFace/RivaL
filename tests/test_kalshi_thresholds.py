"""Kalshi threshold-market recording: the N1 forward sample's Kalshi side.

Hourly public-API snapshots of the Kalshi BTC/ETH threshold markets
carry each contract's terms as the venue states them -- the venue's
own strike_type classifies the contract, its rules persist verbatim,
and its fixed-point dollar strings parse as stated. The recorder
exists because the arm's fallback (the venue's historical
candlesticks archive) has a moving coverage cutoff; it must never
guess a market's terms, and one series' outage must stay local.
"""
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.config import Settings
from parlay_bot.perpetuals.kalshi_thresholds import (
    SERIES,
    KalshiThresholdsRecorder,
    market_row,
)
from parlay_bot.storage.hot import SQLiteHotStore

T0 = datetime(2026, 11, 20, 4, 0, 0, tzinfo=UTC)

SPEC = {spec.series_ticker: spec for spec in SERIES}
ABOVE_BTC = SPEC["KXBTCD"]
RANGE_BTC = SPEC["KXBTC"]
TOUCH_BTC_MONTHLY = SPEC["KXBTCMAXMON"]
TOUCH_BTC_MIN_MONTHLY = SPEC["KXBTCMINMON"]


def _market(**overrides):
    market = {
        "ticker": "KXBTCD-26NOV2000-T73799.99",
        "event_ticker": "KXBTCD-26NOV2000",
        "market_type": "binary",
        "status": "active",
        "title": "Bitcoin price above $73,799.99 on Nov 20, 2026?",
        "yes_sub_title": "$73,799.99",
        "subtitle": "$73,799.99",
        "strike_type": "greater",
        "floor_strike": 73799.99,
        "cap_strike": None,
        "yes_bid_dollars": "0.5600",
        "yes_ask_dollars": "0.5700",
        "no_bid_dollars": "0.4300",
        "no_ask_dollars": "0.4400",
        "last_price_dollars": "0.5600",
        "open_time": "2026-11-19T04:00:00Z",
        "close_time": "2026-11-20T05:00:00Z",
        "expected_expiration_time": "2026-11-20T05:05:00Z",
        "rules_primary": "If the simple average of BRTI is above, resolves Yes.",
        "rules_secondary": "The price used is based on CF Benchmarks' RTI.",
    }
    market.update(overrides)
    return market


def test_series_map_covers_the_design_universe():
    assert len(SERIES) == 14
    assert len({spec.series_ticker for spec in SERIES}) == 14
    families = {(spec.asset, spec.family) for spec in SERIES}
    assert families == {
        ("BTC", "above"), ("ETH", "above"),
        ("BTC", "range"), ("ETH", "range"),
        ("BTC", "touch"), ("ETH", "touch"),
    }
    horizons = {(spec.family, spec.horizon) for spec in SERIES}
    assert ("above", "hourly") in horizons
    assert ("range", "hourly") in horizons and ("range", "annual") in horizons
    assert ("touch", "monthly") in horizons and ("touch", "annual") in horizons


def test_above_market_row_reads_terms_and_prices():
    row = market_row(ABOVE_BTC, _market())
    assert row["contract_type"] == "above"
    assert row["strike_low"] == 73799.99
    assert row["strike_high"] is None
    assert row["yes_bid"] == 0.56
    assert row["yes_ask"] == 0.57
    assert row["no_bid"] == 0.43
    assert row["last_price"] == 0.56
    assert row["horizon"] == "hourly"
    assert row["close_time"] == "2026-11-20T05:00:00Z"
    assert row["rules_primary"].startswith("If the simple average")


def test_range_brackets_and_tails_classify_from_strike_type():
    bracket = market_row(RANGE_BTC, _market(
        ticker="KXBTC-26NOV2000-B73750", strike_type="between",
        floor_strike=73700, cap_strike=73799.99))
    assert bracket["contract_type"] == "range"
    assert bracket["strike_low"] == 73700.0
    assert bracket["strike_high"] == 73799.99
    tail_up = market_row(RANGE_BTC, _market(strike_type="greater"))
    assert tail_up["contract_type"] == "above"
    tail_down = market_row(RANGE_BTC, _market(
        strike_type="less", floor_strike=None, cap_strike=50000))
    assert tail_down["contract_type"] == "below"
    assert tail_down["strike_low"] is None
    assert tail_down["strike_high"] == 50000.0


def test_touch_markets_split_reach_from_dip():
    reach = market_row(TOUCH_BTC_MONTHLY, _market(
        ticker="KXBTCMAXMON-BTC-26NOV30-9750000",
        strike_type="greater", floor_strike=97500, cap_strike=None))
    assert reach["contract_type"] == "reach"
    assert reach["strike_low"] == 97500.0
    assert reach["horizon"] == "monthly"
    dip = market_row(TOUCH_BTC_MIN_MONTHLY, _market(
        ticker="KXBTCMINMON-BTC-26NOV30-5000000",
        strike_type="less", floor_strike=None, cap_strike=50000))
    assert dip["contract_type"] == "dip"
    assert dip["strike_low"] is None
    assert dip["strike_high"] == 50000.0


def test_unparseable_prices_stay_null_never_repaired():
    row = market_row(ABOVE_BTC, _market(
        yes_bid_dollars="not-a-price", yes_ask_dollars="1.2500",
        last_price_dollars=None))
    assert row["yes_bid"] is None
    assert row["yes_ask"] is None  # outside 0..1 is not a probability price
    assert row["last_price"] is None


def test_market_without_identity_or_terms_is_skipped():
    assert market_row(ABOVE_BTC, _market(ticker="")) is None
    assert market_row(ABOVE_BTC, _market(ticker=None)) is None
    assert market_row(ABOVE_BTC, _market(close_time=None)) is None
    assert market_row(ABOVE_BTC, _market(strike_type="functional")) is None
    bracket_no_cap = _market(strike_type="between", cap_strike=None)
    assert market_row(RANGE_BTC, bracket_no_cap) is None
    greater_no_floor = _market(strike_type="greater", floor_strike=None)
    assert market_row(ABOVE_BTC, greater_no_floor) is None


def test_record_markets_persists_and_dedupes(tmp_path):
    recorder = KalshiThresholdsRecorder(
        SQLiteHotStore(tmp_path / "n1.sqlite"))
    markets = [_market(), _market(ticker="KXBTCD-26NOV2000-T73899.99",
                                  floor_strike=73899.99)]
    written, skipped = recorder.record_markets(
        ABOVE_BTC, markets, snapshot_at=T0)
    assert (written, skipped) == (2, 0)
    assert recorder.record_markets(ABOVE_BTC, markets, snapshot_at=T0) == (0, 0)
    rows = recorder.snapshots(asset="BTC", contract_type="above")
    assert len(rows) == 2
    assert rows[0]["snapshot_at"] == T0.isoformat()
    assert rows[0]["series_ticker"] == "KXBTCD"
    assert rows[0]["yes_ask"] == 0.57


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    """Fixture HTTP client: routes Kalshi series queries to canned markets.

    ``pages_by_series`` maps a series ticker to a list of page payloads
    served in order (cursor-chained); ``fail_series`` raises instead.
    """

    def __init__(self, *, pages_by_series, fail_series=()):
        self._pages = pages_by_series
        self._fail = set(fail_series)
        self._served = {}

    async def get(self, url, params=None):
        params = params or {}
        ticker = params["series_ticker"]
        if ticker in self._fail:
            raise httpx.ConnectError("venue down", request=None)
        pages = self._pages.get(ticker, [{"markets": [], "cursor": None}])
        index = self._served.get(ticker, 0)
        self._served[ticker] = index + 1
        return _Response(pages[min(index, len(pages) - 1)])


async def test_capture_records_series_and_isolates_failures(tmp_path):
    recorder = KalshiThresholdsRecorder(
        SQLiteHotStore(tmp_path / "n1.sqlite"))
    client = _Client(
        pages_by_series={"KXBTCD": [{"markets": [_market()], "cursor": None}]},
        fail_series={"KXETHD"},
    )
    receipt = await recorder.capture(client, now=T0)
    assert receipt["KXBTCD"]["rows"] == 1
    assert receipt["KXETHD"] == {"series": "KXETHD", "error": "ConnectError"}
    assert receipt["KXBTCMAXMON"]["rows"] == 0
    rows = recorder.snapshots()
    assert len(rows) == 1
    assert rows[0]["market_ticker"] == "KXBTCD-26NOV2000-T73799.99"


async def test_capture_follows_the_cursor(tmp_path):
    recorder = KalshiThresholdsRecorder(
        SQLiteHotStore(tmp_path / "n1.sqlite"))
    second = _market(ticker="KXBTCD-26NOV2000-T73899.99",
                     floor_strike=73899.99)
    client = _Client(pages_by_series={"KXBTCD": [
        {"markets": [_market()], "cursor": "page-2"},
        {"markets": [second], "cursor": None},
    ]})
    receipt = await recorder.capture(client, now=T0)
    assert receipt["KXBTCD"]["rows"] == 2
    assert receipt["KXBTCD"]["markets"] == 2


def test_kalshi_recorder_config_default():
    fields = Settings.model_fields
    assert fields["rival_n1_kalshi_threshold_enabled"].default is True



def _pg_store():
    """Schema-scoped PostgresHotStore (statement counting is PG-only)."""
    import os
    import uuid
    from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

    import psycopg
    import pytest
    from psycopg import sql

    from parlay_bot.storage.hot import PostgresHotStore

    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL CI service required")
    schema = "n1_batch_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query))
    query["options"] = (query.get("options", "") + f" -c search_path={schema}").strip()
    scoped = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    return PostgresHotStore(scoped), dsn, schema


def _drop_schema(dsn, schema):
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_record_markets_batches_with_identical_contents():
    """250 markets land in 3 chunk statements (was: ~1,270 round trips per
    hourly snapshot across series), with written/skipped counts and table
    contents identical to the per-row semantics."""
    from parlay_bot.storage.database import database_clock

    store, dsn, schema = _pg_store()
    try:
        recorder = KalshiThresholdsRecorder(store)
        markets = [
            _market(ticker=f"KXBTCD-26NOV2000-T{70000 + i}.99",
                    floor_strike=float(70000 + i))
            for i in range(250)
        ]
        markets.append(dict(markets[0]))  # in-batch duplicate: first wins
        markets.append(_market(ticker=""))  # unparseable: skipped, not stored
        recorder.record_markets(ABOVE_BTC, markets[:1], snapshot_at=T0 - timedelta(hours=1))  # warm
        with database_clock() as clock:
            written, skipped = recorder.record_markets(
                ABOVE_BTC, markets, snapshot_at=T0)
        assert clock.statements == 3  # ceil(251 / 100) chunk inserts
        assert (written, skipped) == (250, 1)
        assert recorder.record_markets(
            ABOVE_BTC, markets, snapshot_at=T0) == (0, 1)
        stored = [row for row in recorder.snapshots(asset="BTC", contract_type="above")
                  if row["snapshot_at"] == T0.isoformat()]
        assert len(stored) == 250
        by_ticker = {row["market_ticker"]: row for row in stored}
        for market in markets[:250]:
            want = market_row(ABOVE_BTC, market)
            got = by_ticker[want["market_ticker"]]
            assert got == {"snapshot_at": T0.isoformat(), **want}
    finally:
        store.close()
        _drop_schema(dsn, schema)


def test_snapshots_stream_past_the_chunk_boundary(tmp_path):
    """2,500 stored markets (FETCH_CHUNK=2000) read back complete and in
    snapshot order."""
    recorder = KalshiThresholdsRecorder(SQLiteHotStore(tmp_path / "n1.sqlite"))
    markets = [
        _market(ticker=f"KXBTCD-26NOV2000-T{70000 + i}.99",
                floor_strike=float(70000 + i))
        for i in range(2500)
    ]
    written, skipped = recorder.record_markets(ABOVE_BTC, markets, snapshot_at=T0)
    assert (written, skipped) == (2500, 0)
    stored = recorder.snapshots(asset="BTC", contract_type="above")
    assert len(stored) == 2500
    by_ticker = {row["market_ticker"]: row for row in stored}
    for market in markets:
        want = market_row(ABOVE_BTC, market)
        assert by_ticker[want["market_ticker"]] == {
            "snapshot_at": T0.isoformat(), **want}
