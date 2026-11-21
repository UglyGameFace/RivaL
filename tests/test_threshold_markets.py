"""Polymarket threshold-market recording: the N1 forward sample's market side.

Hourly Gamma snapshots of the Above / Range / Reach / Dip series carry
each contract's terms as Gamma states them and the fee schedule in
force at that hour -- grading reads the recorded schedule, so the
recorder must distinguish a stated 0.07, a stated fee-free market, and
an unknown schedule, and must never guess a market's terms.
"""
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.perpetuals.threshold_markets import (
    SERIES,
    ThresholdMarketsRecorder,
    market_row,
)
from parlay_bot.storage.hot import SQLiteHotStore

T0 = datetime(2026, 11, 20, 4, 0, 0, tzinfo=UTC)
FEE_07 = {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2}

SPEC = {spec.series_slug: spec for spec in SERIES}
ABOVE_BTC = SPEC["btc-multi-strikes-weekly"]
RANGE_BTC = SPEC["bitcoin-neg-risk-weekly"]
HIT_BTC_MONTHLY = SPEC["bitcoin-hit-price-monthly"]


def _market(**overrides):
    market = {
        "id": "7176633",
        "conditionId": "0xabc123",
        "question": "Will the price of Bitcoin be above $52,000 on November 26?",
        "slug": "bitcoin-above-52k-on-november-26-2026",
        "groupItemTitle": "52,000",
        "outcomePrices": "[\"0.9815\", \"0.0185\"]",
        "bestBid": 0.974,
        "bestAsk": 0.989,
        "endDate": "2026-11-26T17:00:00Z",
        "feeSchedule": dict(FEE_07),
        "feesEnabled": True,
    }
    market.update(overrides)
    return market


def _event(markets, **overrides):
    event = {
        "id": "1425704",
        "slug": "bitcoin-above-on-november-26-2026",
        "endDate": "2026-11-26T17:00:00Z",
        "markets": markets,
    }
    event.update(overrides)
    return event


def test_series_map_covers_the_design_universe():
    assert len(SERIES) == 10
    assert len({spec.series_id for spec in SERIES}) == 10
    families = {(spec.asset, spec.family) for spec in SERIES}
    assert families == {
        ("BTC", "above"), ("ETH", "above"),
        ("BTC", "range"), ("ETH", "range"),
        ("BTC", "hit"), ("ETH", "hit"),
    }
    horizons = {(spec.family, spec.horizon) for spec in SERIES}
    assert ("hit", "weekly") in horizons and ("hit", "monthly") in horizons


def test_above_market_row_reads_terms_and_fee():
    row = market_row(ABOVE_BTC, _event([]), _market())
    assert row["contract_type"] == "above"
    assert row["strike_low"] == 52000.0
    assert row["strike_high"] is None
    assert row["yes_price"] == 0.9815
    assert row["best_bid"] == 0.974
    assert row["fee_rate"] == 0.07
    assert row["horizon"] == "daily"
    assert row["end_date"] == "2026-11-26T17:00:00Z"


def test_above_strike_falls_back_to_the_question():
    row = market_row(ABOVE_BTC, _event([]), _market(groupItemTitle=""))
    assert row["strike_low"] == 52000.0


def test_range_brackets_parse_both_bounds():
    def bracket(title, question=""):
        return market_row(RANGE_BTC, _event([]), _market(
            groupItemTitle=title, question=question))

    open_low = bracket("<54,000")
    assert open_low["contract_type"] == "range"
    assert open_low["strike_low"] is None
    assert open_low["strike_high"] == 54000.0
    middle = bracket("54,000-56,000")
    assert middle["strike_low"] == 54000.0
    assert middle["strike_high"] == 56000.0
    open_high = bracket(">72,000")
    assert open_high["strike_low"] == 72000.0
    assert open_high["strike_high"] is None


def test_hit_markets_split_reach_from_dip_by_question():
    reach = market_row(HIT_BTC_MONTHLY, _event([], slug="h"), _market(
        question="Will Bitcoin reach $72,500 in November?",
        groupItemTitle="$72,500",
        outcomePrices=["0.25", "0.75"]))
    assert reach["contract_type"] == "reach"
    assert reach["strike_low"] == 72500.0
    assert reach["yes_price"] == 0.25  # list form parses like the JSON text
    dip = market_row(HIT_BTC_MONTHLY, _event([], slug="h"), _market(
        question="Will Bitcoin dip to $55,000 in November?",
        groupItemTitle="$55,000"))
    assert dip["contract_type"] == "dip"
    assert dip["strike_low"] is None
    assert dip["strike_high"] == 55000.0


def test_monthly_series_annual_event_is_classified_annual():
    annual = _event([], slug="what-price-will-bitcoin-hit-before-2027")
    row = market_row(HIT_BTC_MONTHLY, annual, _market(
        question="Will Bitcoin reach $150,000 before 2027?",
        groupItemTitle="$150,000"))
    assert row["horizon"] == "annual"
    monthly = _event([], slug="what-price-will-bitcoin-hit-in-november-2026")
    row = market_row(HIT_BTC_MONTHLY, monthly, _market(
        question="Will Bitcoin reach $100,000 in November?",
        groupItemTitle="$100,000"))
    assert row["horizon"] == "monthly"


def test_fee_states_are_never_conflated():
    stated = market_row(ABOVE_BTC, _event([]), _market())
    assert stated["fee_rate"] == 0.07
    free = market_row(ABOVE_BTC, _event([]), _market(
        feeSchedule=None, feesEnabled=False))
    assert free["fee_rate"] == 0.0  # the venue positively states: no fee
    assert free["fee_schedule_json"] is None
    unknown = market_row(ABOVE_BTC, _event([]), _market(
        feeSchedule=None, feesEnabled=True))
    assert unknown["fee_rate"] is None  # unknown stays unknown


def test_market_without_identity_or_terms_is_skipped():
    assert market_row(ABOVE_BTC, _event([]), _market(conditionId="")) is None
    assert market_row(ABOVE_BTC, _event([]), _market(conditionId=None)) is None
    hit_no_verb = _market(question="Will Bitcoin print $72,500 in November?",
                          groupItemTitle="$72,500")
    assert market_row(HIT_BTC_MONTHLY, _event([]), hit_no_verb) is None


def test_record_event_persists_and_dedupes(tmp_path):
    recorder = ThresholdMarketsRecorder(
        SQLiteHotStore(tmp_path / "n1.sqlite"))
    event = _event([_market(), _market(
        conditionId="0xdef456", groupItemTitle="54,000",
        question="Will the price of Bitcoin be above $54,000 on November 26?")])
    written, skipped = recorder.record_event(ABOVE_BTC, event, snapshot_at=T0)
    assert (written, skipped) == (2, 0)
    assert recorder.record_event(ABOVE_BTC, event, snapshot_at=T0) == (0, 0)
    rows = recorder.snapshots(asset="BTC", contract_type="above")
    assert len(rows) == 2
    assert rows[0]["snapshot_at"] == T0.isoformat()
    assert rows[0]["series_slug"] == "btc-multi-strikes-weekly"
    assert rows[0]["fee_schedule_json"] is not None


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    """Fixture HTTP client: routes Gamma series queries to canned events."""

    def __init__(self, *, events_by_series, fail_series=()):
        self._events = events_by_series
        self._fail = set(fail_series)

    async def get(self, url, params=None):
        params = params or {}
        series_id = params["series_id"]
        if series_id in self._fail:
            raise httpx.ConnectError("venue down", request=None)
        if params.get("offset"):
            return _Response([])
        return _Response(self._events.get(series_id, []))


async def test_capture_records_series_and_isolates_failures(tmp_path):
    recorder = ThresholdMarketsRecorder(
        SQLiteHotStore(tmp_path / "n1.sqlite"))
    client = _Client(
        events_by_series={45: [_event([_market()])]},
        fail_series={42},
    )
    receipt = await recorder.capture(client, now=T0)
    assert receipt["btc-multi-strikes-weekly"]["rows"] == 1
    assert receipt["ethereum-multi-strikes-weekly"] == {
        "series": "ethereum-multi-strikes-weekly", "error": "ConnectError"}
    assert receipt["bitcoin-hit-price-monthly"]["rows"] == 0
    rows = recorder.snapshots()
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "0xabc123"



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


def test_record_event_batches_with_identical_contents():
    """250 markets land in 3 chunk statements (was: one round trip per
    market), with written/skipped counts and table contents identical to
    the per-row semantics."""
    from parlay_bot.storage.database import database_clock

    store, dsn, schema = _pg_store()
    try:
        recorder = ThresholdMarketsRecorder(store)
        markets = [
            _market(id=str(7176633 + i), conditionId=f"0x{i:06x}",
                    groupItemTitle=f"{52000 + i:,}",
                    question=f"Will the price of Bitcoin be above "
                             f"${52000 + i:,} on November 26?")
            for i in range(250)
        ]
        markets.append(dict(markets[0]))  # in-batch duplicate: first wins
        recorder.record_event(ABOVE_BTC, _event(markets[:1]), snapshot_at=T0 - timedelta(hours=1))
        with database_clock() as clock:
            written, skipped = recorder.record_event(
                ABOVE_BTC, _event(markets), snapshot_at=T0)
        assert clock.statements == 3  # ceil(251 / 100) chunk inserts
        assert (written, skipped) == (250, 0)
        assert recorder.record_event(
            ABOVE_BTC, _event(markets), snapshot_at=T0) == (0, 0)
        stored = [row for row in recorder.snapshots(asset="BTC", contract_type="above")
                  if row["snapshot_at"] == T0.isoformat()]
        assert len(stored) == 250
        by_condition = {row["condition_id"]: row for row in stored}
        event = _event(markets)
        for market in markets[:250]:
            want = market_row(ABOVE_BTC, event, market)
            got = by_condition[want["condition_id"]]
            assert got == {"snapshot_at": T0.isoformat(), **want}
    finally:
        store.close()
        _drop_schema(dsn, schema)
