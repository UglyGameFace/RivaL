"""Executable book snapshots for the PM-lag convergence re-measurement.

The collection is fail-closed: stale, future, malformed or empty books
never become rows, depth averages exist only when the book can actually
fill the reference clip, and checkpoint selection never reaches outside
its pre-registered tolerance.
"""
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.config import Settings
from parlay_bot.perpetuals.convergence_quotes import (
    ConvergenceQuotes,
    clob_snapshot,
    kalshi_snapshot,
    select_checkpoint,
)
from parlay_bot.storage.hot import SQLiteHotStore

RECEIVED = datetime(2026, 11, 19, 12, 5, 0, tzinfo=UTC)
OPEN = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
CLOSE = OPEN + timedelta(minutes=15)


def _clob_book(*, stamp=None, bids=None, asks=None):
    return {
        "timestamp": str(int((stamp or RECEIVED - timedelta(seconds=2)).timestamp() * 1000)),
        "bids": bids if bids is not None else [
            {"price": "0.50", "size": "3"}, {"price": "0.52", "size": "9"}],
        "asks": asks if asks is not None else [
            {"price": "0.55", "size": "4"}, {"price": "0.53", "size": "8"},
            {"price": "0.60", "size": "50"}],
    }


def test_clob_snapshot_best_and_clip_depth():
    snap = clob_snapshot(_clob_book(), received_at=RECEIVED)
    assert snap is not None
    assert snap["best_ask"] == pytest.approx(0.53)
    assert snap["ask_size"] == pytest.approx(8.0)
    assert snap["best_bid"] == pytest.approx(0.52)
    assert snap["bid_size"] == pytest.approx(9.0)
    # 10-share depth-walked averages across two levels each side.
    assert snap["avg_ask"] == pytest.approx((8 * 0.53 + 2 * 0.55) / 10)
    assert snap["avg_bid"] == pytest.approx((9 * 0.52 + 1 * 0.50) / 10)
    assert snap["source_at"] == RECEIVED - timedelta(seconds=2)


def test_clob_snapshot_thin_book_has_best_but_no_clip_average():
    snap = clob_snapshot(_clob_book(asks=[{"price": "0.53", "size": "5"}]),
                         received_at=RECEIVED)
    assert snap["best_ask"] == pytest.approx(0.53)
    assert snap["avg_ask"] is None


def test_clob_snapshot_one_sided_book_keeps_the_known_side():
    snap = clob_snapshot(_clob_book(asks=[]), received_at=RECEIVED)
    assert snap["best_bid"] == pytest.approx(0.52)
    assert snap["best_ask"] is None and snap["avg_ask"] is None


@pytest.mark.parametrize("book", [
    _clob_book(stamp=RECEIVED - timedelta(seconds=26)),   # stale
    _clob_book(stamp=RECEIVED + timedelta(seconds=6)),    # future
    {"bids": [{"price": "0.5", "size": "1"}], "asks": []},  # no timestamp
    _clob_book(bids=[], asks=[]),                          # empty
    {"timestamp": "not-a-number", "bids": [], "asks": []},
    "not-a-book",
])
def test_clob_snapshot_rejects_unusable_books(book):
    assert clob_snapshot(book, received_at=RECEIVED) is None


def test_clob_snapshot_skips_malformed_levels():
    snap = clob_snapshot(_clob_book(asks=[
        {"price": "x", "size": "1"}, {"price": "0.53", "size": "8"},
        {"price": "0.51", "size": "-2"}, {"price": "0.54", "size": "9"},
    ]), received_at=RECEIVED)
    assert snap["best_ask"] == pytest.approx(0.53)
    assert snap["avg_ask"] == pytest.approx((8 * 0.53 + 2 * 0.54) / 10)


def _kalshi_book(yes=None, no=None):
    return {"orderbook_fp": {
        "yes_dollars": yes if yes is not None else [["0.3800", "11.0"], ["0.3000", "20.0"]],
        "no_dollars": no if no is not None else [["0.4600", "17.0"], ["0.5000", "3.0"]],
    }}


def test_kalshi_snapshot_derives_yes_ask_from_no_bids_with_depth():
    snap = kalshi_snapshot(_kalshi_book(), received_at=RECEIVED)
    assert snap is not None
    assert snap["best_bid"] == pytest.approx(0.38)
    assert snap["bid_size"] == pytest.approx(11.0)
    assert snap["avg_bid"] == pytest.approx(0.38)  # top level fills the clip
    # YES ask is the complement of the best NO bid (0.50 -> 0.50, size 3),
    # then the next NO level (0.46 -> 0.54, size 17).
    assert snap["best_ask"] == pytest.approx(0.50)
    assert snap["ask_size"] == pytest.approx(3.0)
    assert snap["avg_ask"] == pytest.approx((3 * 0.50 + 7 * 0.54) / 10)
    assert snap["source_at"] == RECEIVED


def test_kalshi_snapshot_thin_book_has_no_clip_average():
    snap = kalshi_snapshot(_kalshi_book(no=[["0.4600", "4.0"]]), received_at=RECEIVED)
    assert snap["best_ask"] == pytest.approx(0.54)
    assert snap["avg_ask"] is None


@pytest.mark.parametrize("payload", [
    {},
    {"orderbook_fp": {"yes_dollars": []}},                       # missing side key rows ok? no: missing key raises
    _kalshi_book(yes=[[0.38, 11.0]]),                            # non-string fields
    _kalshi_book(yes=[["1.50", "1.00"]]),                        # price out of range
    _kalshi_book(no=[["0.50", "0.00"]]),                         # zero depth
    _kalshi_book(yes=[], no=[]),                                 # empty
    "not-a-book",
])
def test_kalshi_snapshot_rejects_unusable_books(payload):
    assert kalshi_snapshot(payload, received_at=RECEIVED) is None


def test_store_roundtrip_dedupes_and_keeps_venues_separate(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    quotes.initialize()
    quotes.initialize()  # idempotent
    book = _clob_book()
    assert quotes.record_polymarket(
        asset="BTC", slug="btc-updown-15m-1", window_open=OPEN,
        window_close=CLOSE, fee_rate=0.07, book=book, received_at=RECEIVED)
    # The same book time for the same contract is one snapshot, not two.
    assert not quotes.record_polymarket(
        asset="BTC", slug="btc-updown-15m-1", window_open=OPEN,
        window_close=CLOSE, fee_rate=0.07, book=book, received_at=RECEIVED)
    assert quotes.record_kalshi(
        asset="BTC", ticker="KXBTC15M-26NOV190700-00", window_open=OPEN,
        window_close=CLOSE, payload=_kalshi_book(), received_at=RECEIVED)
    pm_rows = quotes.snapshots(venue="polymarket", contract_id="btc-updown-15m-1")
    assert len(pm_rows) == 1
    row = pm_rows[0]
    assert row["asset"] == "BTC"
    assert row["best_ask"] == pytest.approx(0.53)
    assert row["avg_bid"] == pytest.approx((9 * 0.52 + 1 * 0.50) / 10)
    assert row["fee_rate"] == pytest.approx(0.07)
    kalshi_rows = quotes.snapshots(venue="kalshi", contract_id="KXBTC15M-26NOV190700-00")
    assert len(kalshi_rows) == 1
    assert kalshi_rows[0]["fee_rate"] is None


def test_store_drops_unusable_books_without_rows(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    stale = _clob_book(stamp=RECEIVED - timedelta(minutes=5))
    assert not quotes.record_polymarket(
        asset="BTC", slug="s", window_open=OPEN, window_close=CLOSE,
        fee_rate=0.07, book=stale, received_at=RECEIVED)
    assert quotes.snapshots(venue="polymarket", contract_id="s") == []


def test_due_throttles_per_contract(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    t0 = RECEIVED
    assert quotes.due("polymarket", "a", t0, cooldown_seconds=30)
    assert not quotes.due("polymarket", "a", t0 + timedelta(seconds=10),
                          cooldown_seconds=30)
    assert quotes.due("polymarket", "b", t0 + timedelta(seconds=10),
                      cooldown_seconds=30)
    assert quotes.due("kalshi", "a", t0 + timedelta(seconds=10),
                      cooldown_seconds=30)
    assert quotes.due("polymarket", "a", t0 + timedelta(seconds=31),
                      cooldown_seconds=30)


def _row(seconds_in):
    return {"source_at": (OPEN + timedelta(seconds=seconds_in)).isoformat()}


def test_select_checkpoint_picks_closest_within_tolerance():
    rows = [_row(290), _row(318), _row(400)]
    picked = select_checkpoint(rows, window_open=OPEN, offset_seconds=300)
    assert picked is _row(290) or picked["source_at"] == _row(290)["source_at"]


def test_select_checkpoint_tie_goes_to_the_earlier_snapshot():
    rows = [_row(320), _row(280)]
    picked = select_checkpoint(rows, window_open=OPEN, offset_seconds=300)
    assert picked["source_at"] == _row(280)["source_at"]


def test_select_checkpoint_outside_tolerance_is_missing_not_approximated():
    rows = [_row(200), _row(352)]
    assert select_checkpoint(rows, window_open=OPEN, offset_seconds=300) is None
    assert select_checkpoint([], window_open=OPEN, offset_seconds=300) is None


def test_collection_settings_defaults():
    settings = Settings()
    assert settings.rival_convergence_quotes_enabled is True
    assert settings.rival_convergence_quotes_seconds == 30


def test_record_polymarket_persists_reference_level(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    assert quotes.record_polymarket(
        asset="BTC", slug="btc-updown-15m-1", window_open=OPEN,
        window_close=CLOSE, fee_rate=0.07, book=_clob_book(),
        received_at=RECEIVED, reference_level=100_000.0)
    row = quotes.snapshots(venue="polymarket", contract_id="btc-updown-15m-1")[0]
    assert row["reference_level"] == pytest.approx(100_000.0)


def test_record_kalshi_persists_reference_level(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    assert quotes.record_kalshi(
        asset="BTC", ticker="KXBTC15M-1", window_open=OPEN,
        window_close=CLOSE, payload=_kalshi_book(), received_at=RECEIVED,
        reference_level=100_050.0)
    row = quotes.snapshots(venue="kalshi", contract_id="KXBTC15M-1")[0]
    assert row["reference_level"] == pytest.approx(100_050.0)


def test_reference_level_is_cached_per_contract(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    assert quotes.record_polymarket(
        asset="BTC", slug="btc-updown-15m-1", window_open=OPEN,
        window_close=CLOSE, fee_rate=0.07, book=_clob_book(),
        received_at=RECEIVED, reference_level=100_000.0)
    # A later snapshot of the same contract whose fetch lost the Gamma
    # metadata still carries the level the first snapshot resolved --
    # the reference is static for the life of the market.
    later = RECEIVED + timedelta(seconds=30)
    assert quotes.record_polymarket(
        asset="BTC", slug="btc-updown-15m-1", window_open=OPEN,
        window_close=CLOSE, fee_rate=0.07,
        book=_clob_book(stamp=later), received_at=later)
    rows = quotes.snapshots(venue="polymarket", contract_id="btc-updown-15m-1")
    assert len(rows) == 2
    assert rows[1]["reference_level"] == pytest.approx(100_000.0)
    # A different contract does not inherit it.
    assert quotes.record_polymarket(
        asset="BTC", slug="btc-updown-15m-2", window_open=OPEN,
        window_close=CLOSE, fee_rate=0.07,
        book=_clob_book(stamp=later), received_at=later)
    other = quotes.snapshots(venue="polymarket", contract_id="btc-updown-15m-2")
    assert other[0]["reference_level"] is None


@pytest.mark.parametrize("bad", [
    None, 0.0, -1.0, float("nan"), float("inf"), "100000.0", True,
])
def test_unusable_reference_levels_stay_unknown(tmp_path, bad):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "conv.sqlite"))
    assert quotes.record_polymarket(
        asset="BTC", slug="s", window_open=OPEN, window_close=CLOSE,
        fee_rate=0.07, book=_clob_book(), received_at=RECEIVED,
        reference_level=bad)
    row = quotes.snapshots(venue="polymarket", contract_id="s")[0]
    assert row["reference_level"] is None


_PRE_REPAIR_SCHEMA = """
CREATE TABLE rival_convergence_book_snapshots (
    venue TEXT NOT NULL, asset TEXT NOT NULL, contract_id TEXT NOT NULL,
    window_open TEXT NOT NULL, window_close TEXT NOT NULL,
    source_at TEXT NOT NULL, received_at TEXT NOT NULL,
    best_bid DOUBLE PRECISION, best_ask DOUBLE PRECISION,
    bid_size DOUBLE PRECISION, ask_size DOUBLE PRECISION,
    avg_bid DOUBLE PRECISION, avg_ask DOUBLE PRECISION,
    fee_rate DOUBLE PRECISION, book_sha256 TEXT NOT NULL,
    PRIMARY KEY (venue, contract_id, source_at)
);
"""


def test_initialize_adds_reference_level_to_pre_repair_tables(tmp_path):
    store = SQLiteHotStore(tmp_path / "conv.sqlite")
    with store.connect() as conn:
        conn.executescript(_PRE_REPAIR_SCHEMA)
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    quotes.initialize()  # the extension is idempotent too
    with store.connect() as conn:
        present = store.database.column_names(
            conn, "rival_convergence_book_snapshots")
    assert "reference_level" in present
    # And the extended table records references like a fresh one.
    assert quotes.record_polymarket(
        asset="BTC", slug="s", window_open=OPEN, window_close=CLOSE,
        fee_rate=0.07, book=_clob_book(), received_at=RECEIVED,
        reference_level=100_000.0)
    row = quotes.snapshots(venue="polymarket", contract_id="s")[0]
    assert row["reference_level"] == pytest.approx(100_000.0)
