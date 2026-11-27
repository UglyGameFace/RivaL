"""PR2 convergence evaluator: frozen criteria, accrual guard, three cuts.

All fixtures are synthetic snapshot rows in the stored shape. The
evaluator is read-only and pure over these inputs; the only I/O test is
``load_windows`` against a throwaway SQLite store.
"""
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.convergence_evaluation import (
    ACCRUAL_DEADLINE,
    FEE_CHANGE_AT,
    MIN_DISTINCT_DAYS,
    MIN_TRIGGER_WINDOWS,
    PASS_MEAN_NET,
    SECONDARY_FEE_ONLY_GAP,
    STRIKE_ALIGNMENT_TOLERANCE_REL,
    TRIGGER_GAP,
    evaluate_windows,
    kalshi_reference_fee,
    load_windows,
)
from parlay_bot.perpetuals.convergence_quotes import ConvergenceQuotes
from parlay_bot.storage.hot import SQLiteHotStore

OPEN = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)  # post-fee-change window
NOW = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)


def _snap(opened, offset, *, best_bid=None, best_ask=None, avg_bid=None,
          avg_ask=None, fee_rate=None, sha="s"):
    return {
        "source_at": (opened + timedelta(seconds=offset)).isoformat(),
        "received_at": (opened + timedelta(seconds=offset)).isoformat(),
        "best_bid": best_bid, "best_ask": best_ask,
        "avg_bid": avg_bid, "avg_ask": avg_ask,
        "fee_rate": fee_rate, "book_sha256": sha,
    }


def _window(opened=OPEN, *, asset="ETH", entry_ask=0.40, exit_bid=0.46,
            kalshi_bid=0.47, kalshi_ask=0.49, fee_rate=0.01,
            pm_ref=100_000.0, kalshi_ref=100_050.0, entry_offset=300):
    pm = [
        _snap(opened, entry_offset, best_bid=0.39, best_ask=0.41,
              avg_bid=0.39, avg_ask=entry_ask, fee_rate=fee_rate, sha="p1"),
        _snap(opened, 600, best_bid=0.45, best_ask=0.47,
              avg_bid=exit_bid, avg_ask=0.47, fee_rate=fee_rate, sha="p2"),
    ]
    kalshi = [
        _snap(opened, entry_offset - 30, best_bid=0.40, best_ask=0.42,
              avg_bid=0.40, avg_ask=0.42, sha="k1"),
        _snap(opened, entry_offset, best_bid=kalshi_bid, best_ask=kalshi_ask,
              avg_bid=kalshi_bid, avg_ask=kalshi_ask, sha="k2"),
    ]
    return {
        "asset": asset, "window_open": opened,
        "pm_snapshots": pm, "kalshi_snapshots": kalshi,
        "pm_reference": pm_ref, "kalshi_reference": kalshi_ref,
    }


def test_frozen_criteria_constants_do_not_drift():
    # Changing any of these is a new pre-registration, not an edit.
    assert TRIGGER_GAP == 0.035
    assert PASS_MEAN_NET == 0.005
    assert MIN_TRIGGER_WINDOWS == 300
    assert MIN_DISTINCT_DAYS == 7
    assert SECONDARY_FEE_ONLY_GAP == 0.0175
    assert STRIKE_ALIGNMENT_TOLERANCE_REL == 0.0020
    assert FEE_CHANGE_AT == datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    assert ACCRUAL_DEADLINE == datetime(2026, 12, 10, 2, 12, tzinfo=UTC)


def test_trigger_math_and_per_contract_fees():
    report = evaluate_windows([_window()], now=NOW)
    pooled = report["cuts"]["pooled"]
    assert report["coverage"]["trigger_windows"] == 1
    # fee 0.01: entry 0.01*.40*.60 = .0024; exit 0.01*.46*.54 = .002484
    assert pooled["mean_net_per_contract"] == pytest.approx(
        0.46 - 0.40 - 0.0024 - 0.002484)
    assert pooled["win_rate"] == 1.0
    # Below the frozen evaluation point: accrual state, NO verdict.
    assert report["status"] == "ACCRUAL"
    assert report["verdict"] is None


def test_pm_fee_uses_each_contracts_own_recorded_rate():
    # BTC post-change rate 0.05 instead of 0.01: both legs re-priced.
    report = evaluate_windows([_window(asset="BTC", fee_rate=0.05)], now=NOW)
    pooled = report["cuts"]["pooled"]
    assert pooled["mean_net_per_contract"] == pytest.approx(
        0.46 - 0.40 - 0.05 * 0.40 * 0.60 - 0.05 * 0.46 * 0.54)


def test_kalshi_reference_fee_uses_kalshi_fee_with_cent_rounding():
    # Kalshi snapshots store fee_rate=None; the toll comes from kalshi_fee:
    # 0.07 * .5 * .5 = .0175, rounded UP to the next cent.
    assert kalshi_reference_fee(0.5) == pytest.approx(0.02)
    assert kalshi_reference_fee(0.48) == pytest.approx(
        __import__("math").ceil(round(0.07 * 0.48 * 0.52 * 100, 6)) / 100)


def test_below_trigger_is_counted_not_traded():
    # Kalshi mid 0.43 - PM avg ask 0.40 = 0.03: under the 3.5c trigger,
    # over the 1.75c fee-only line (reported as a count, never a trigger).
    report = evaluate_windows(
        [_window(kalshi_bid=0.42, kalshi_ask=0.44)], now=NOW)
    assert report["coverage"]["trigger_windows"] == 0
    assert report["coverage"]["non_trigger_windows"] == 1
    assert report["coverage"]["secondary_fee_only_line_triggers"] == 1


def test_trigger_boundary_is_strictly_greater():
    # Gap exactly 0.035 does NOT trigger (frozen rule is > 0.035).
    report = evaluate_windows(
        [_window(kalshi_bid=0.425, kalshi_ask=0.445)], now=NOW)
    assert report["coverage"]["trigger_windows"] == 0


def test_unfillable_entry_and_exit_legs_excluded_and_counted():
    entry = _window()
    entry["pm_snapshots"][0]["avg_ask"] = None  # book cannot fill 10 shares
    exit_ = _window()
    exit_["pm_snapshots"][1]["avg_bid"] = None
    report = evaluate_windows([entry, exit_], now=NOW)
    assert report["coverage"]["exclusions"]["unfillable_leg"] == 2
    assert report["coverage"]["trigger_windows"] == 0


def test_strike_divergence_is_a_basis_event_not_a_trigger():
    # Kalshi strike 1% above PM's price-to-beat: same UTC window, but the
    # contracts are pricing different levels -- excluded and counted.
    report = evaluate_windows([_window(kalshi_ref=101_000.0)], now=NOW)
    assert report["coverage"]["exclusions"]["strike_divergence"] == 1
    assert report["coverage"]["trigger_windows"] == 0
    assert report["coverage"]["strike_divergence_rel"]["max"] > 0.009


def test_strike_unavailable_is_excluded_never_proxied():
    report = evaluate_windows([_window(pm_ref=None)], now=NOW)
    assert report["coverage"]["exclusions"]["strike_unavailable"] == 1
    assert report["coverage"]["trigger_windows"] == 0


def test_fee_unverified_window_is_out_of_the_headline():
    report = evaluate_windows([_window(fee_rate=None)], now=NOW)
    assert report["coverage"]["exclusions"]["fee_unverified"] == 1
    assert report["coverage"]["trigger_windows"] == 0
    assert report["coverage"]["fee_verified_rate"] == 0.0


def test_missing_checkpoint_is_counted_per_checkpoint():
    window = _window()
    window["pm_snapshots"] = window["pm_snapshots"][:1]  # no exit snapshot
    report = evaluate_windows([window], now=NOW)
    assert report["coverage"]["exclusions"]["missing_snapshot"] == 1
    assert report["coverage"]["checkpoint_missing"]["pm_exit"] == 1


def test_stale_kalshi_reference_is_excluded_and_age_reported():
    fresh = evaluate_windows([_window()], now=NOW)
    assert fresh["coverage"]["kalshi_reference_book_age_seconds"]["mean"] == 30.0
    stale_window = _window(entry_offset=340)
    for row in stale_window["kalshi_snapshots"]:
        row["book_sha256"] = "same"  # book never changed since the open
    report = evaluate_windows([stale_window], now=NOW)
    assert report["coverage"]["exclusions"]["stale_kalshi_reference"] == 1
    assert report["coverage"]["trigger_windows"] == 0


def test_three_reporting_cuts_pooled_per_asset_post_fee_change():
    pre = _window(opened=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
                  asset="BTC", fee_rate=0.07)
    post = _window(asset="ETH")
    report = evaluate_windows([pre, post], now=NOW)
    assert report["cuts"]["pooled"]["n"] == 2
    assert report["cuts"]["per_asset"]["BTC"]["n"] == 1
    assert report["cuts"]["per_asset"]["ETH"]["n"] == 1
    assert report["cuts"]["per_asset"]["SOL"]["n"] == 0
    # Only the Nov-21 window opens after the 2026-11-20T23:59Z fee change.
    assert report["cuts"]["post_fee_change_only"]["n"] == 1
    assert report["cuts"]["post_fee_change_only"]["mean_net_per_contract"] == \
        report["cuts"]["per_asset"]["ETH"]["mean_net_per_contract"]


def _mature_windows(*, exit_bid=0.46, days=7, per_day=43):
    windows = []
    for day in range(days):
        base = datetime(2026, 11, 21, tzinfo=UTC) + timedelta(days=day)
        for i in range(per_day):
            windows.append(_window(
                opened=base + timedelta(minutes=15 * i),
                exit_bid=exit_bid, asset="ETH"))
    return windows


def test_accrual_guard_refuses_verdict_below_threshold():
    windows = _mature_windows(per_day=42)  # 294 triggers over 7 days
    report = evaluate_windows(windows, now=NOW + timedelta(days=7))
    assert report["coverage"]["trigger_windows"] == 294
    assert report["status"] == "ACCRUAL"
    assert report["verdict"] is None


def test_verdict_pass_at_frozen_evaluation_point():
    windows = _mature_windows()  # 301 triggers over 7 distinct days
    report = evaluate_windows(windows, now=NOW + timedelta(days=7))
    assert report["coverage"]["trigger_windows"] >= MIN_TRIGGER_WINDOWS
    assert report["coverage"]["trigger_distinct_days"] >= MIN_DISTINCT_DAYS
    assert report["status"] == "EVALUATED"
    assert report["verdict"] == "PASS"
    halves = report["cuts"]["pooled"]["chronological_halves"]
    assert halves["first_mean"] > 0 and halves["second_mean"] > 0


def test_verdict_kill_when_a_chronological_half_is_negative():
    windows = _mature_windows()
    # Second half of the sample loses: exit bid below the entry ask.
    ordered = sorted(windows, key=lambda w: w["window_open"])
    for window in ordered[len(ordered) // 2:]:
        window["pm_snapshots"][1]["avg_bid"] = 0.30
    report = evaluate_windows(windows, now=NOW + timedelta(days=7))
    assert report["status"] == "EVALUATED"
    assert report["verdict"] == "KILL"


def test_accrual_failure_after_21_days_without_minimums():
    report = evaluate_windows(
        [_window()], now=ACCRUAL_DEADLINE + timedelta(seconds=1))
    assert report["status"] == "ACCRUAL_FAILED"
    assert report["verdict"] is None


def test_load_windows_pairs_snapshots_and_references(tmp_path):
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    book = {
        "timestamp": str(int((OPEN + timedelta(seconds=298)).timestamp() * 1000)),
        "bids": [{"price": "0.39", "size": "20"}],
        "asks": [{"price": "0.41", "size": "20"}],
    }
    assert quotes.record_polymarket(
        asset="ETH", slug="eth-updown-15m-1", window_open=OPEN,
        window_close=OPEN + timedelta(minutes=15), fee_rate=0.01,
        book=book, received_at=OPEN + timedelta(seconds=300))
    payload = {"orderbook_fp": {
        "yes_dollars": [["0.4700", "20.0"]], "no_dollars": [["0.5100", "20.0"]]}}
    assert quotes.record_kalshi(
        asset="ETH", ticker="KXETH15M-1", window_open=OPEN,
        window_close=OPEN + timedelta(minutes=15), payload=payload,
        received_at=OPEN + timedelta(seconds=300))
    with store.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rival_kalshi_updown ("
            "ticker TEXT PRIMARY KEY, asset TEXT NOT NULL,"
            " opens_at TEXT NOT NULL, closes_at TEXT NOT NULL,"
            " open_reference DOUBLE PRECISION, issued_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO rival_kalshi_updown VALUES (?,?,?,?,?,?)",
            ("KXETH15M-1", "ETH", OPEN.isoformat(),
             (OPEN + timedelta(minutes=15)).isoformat(), 100_050.0,
             OPEN.isoformat()))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rival_poly_oracle_observations ("
            "slug TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO rival_poly_oracle_observations VALUES (?,?)",
            ("eth-updown-15m-1", '{"price_to_beat": 100000.0}'))
    windows = load_windows(store)
    assert len(windows) == 1
    window = windows[0]
    assert window["pm_reference"] == 100_000.0
    assert window["kalshi_reference"] == 100_050.0
    assert len(window["pm_snapshots"]) == 1
    assert len(window["kalshi_snapshots"]) == 1


def _record_full_window(quotes, *, pm_reference=None, kalshi_reference=None):
    """One complete ETH window recorded through the real collector.

    PM entry book at +300 (avg ask 0.40) and exit book at +600 (avg bid
    0.46); two Kalshi books so the entry book at +300 has a measurable
    age against the different book at +270 (mid 0.48 at entry).
    """
    close = OPEN + timedelta(minutes=15)
    entry_book = {
        "timestamp": str(int((OPEN + timedelta(seconds=298)).timestamp() * 1000)),
        "bids": [{"price": "0.39", "size": "20"}],
        "asks": [{"price": "0.40", "size": "20"}],
    }
    assert quotes.record_polymarket(
        asset="ETH", slug="eth-updown-15m-1", window_open=OPEN,
        window_close=close, fee_rate=0.01, book=entry_book,
        received_at=OPEN + timedelta(seconds=300),
        reference_level=pm_reference)
    exit_book = {
        "timestamp": str(int((OPEN + timedelta(seconds=598)).timestamp() * 1000)),
        "bids": [{"price": "0.46", "size": "20"}],
        "asks": [{"price": "0.47", "size": "20"}],
    }
    assert quotes.record_polymarket(
        asset="ETH", slug="eth-updown-15m-1", window_open=OPEN,
        window_close=close, fee_rate=0.01, book=exit_book,
        received_at=OPEN + timedelta(seconds=600),
        reference_level=pm_reference)
    early = {"orderbook_fp": {
        "yes_dollars": [["0.4000", "20.0"]],
        "no_dollars": [["0.5800", "20.0"]]}}
    assert quotes.record_kalshi(
        asset="ETH", ticker="KXETH15M-1", window_open=OPEN,
        window_close=close, payload=early,
        received_at=OPEN + timedelta(seconds=270),
        reference_level=kalshi_reference)
    entry = {"orderbook_fp": {
        "yes_dollars": [["0.4700", "20.0"]],
        "no_dollars": [["0.5100", "20.0"]]}}
    assert quotes.record_kalshi(
        asset="ETH", ticker="KXETH15M-1", window_open=OPEN,
        window_close=close, payload=entry,
        received_at=OPEN + timedelta(seconds=300),
        reference_level=kalshi_reference)


def test_load_windows_pairs_from_snapshot_carried_references(tmp_path):
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    # No evidence tables exist at all: the snapshot-carried levels are
    # the only reference source, and they pair the window end to end.
    _record_full_window(quotes, pm_reference=100_000.0,
                        kalshi_reference=100_050.0)
    windows = load_windows(store)
    assert len(windows) == 1
    assert windows[0]["pm_reference"] == 100_000.0
    assert windows[0]["kalshi_reference"] == 100_050.0
    report = evaluate_windows(windows, now=NOW)
    assert report["coverage"]["exclusions"]["strike_unavailable"] == 0
    assert report["coverage"]["trigger_windows"] == 1


def test_load_windows_snapshot_references_outrank_evidence_joins(tmp_path):
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    _record_full_window(quotes, pm_reference=100_000.0,
                        kalshi_reference=100_050.0)
    with store.connect() as conn:
        conn.execute(
            "CREATE TABLE rival_kalshi_updown ("
            "ticker TEXT PRIMARY KEY, asset TEXT NOT NULL,"
            " opens_at TEXT NOT NULL, closes_at TEXT NOT NULL,"
            " open_reference DOUBLE PRECISION, issued_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO rival_kalshi_updown VALUES (?,?,?,?,?,?)",
            ("KXETH15M-1", "ETH", OPEN.isoformat(),
             (OPEN + timedelta(minutes=15)).isoformat(), 101_500.0,
             OPEN.isoformat()))
        conn.execute(
            "CREATE TABLE rival_poly_oracle_observations ("
            "slug TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO rival_poly_oracle_observations VALUES (?,?)",
            ("eth-updown-15m-1", '{"price_to_beat": 90000.0}'))
    # The join values (90,000 vs 101,500) diverge far beyond the frozen
    # tolerance, as does any mix; only the carried pair (100,000 vs
    # 100,050) aligns, so a trigger proves both carried levels won.
    windows = load_windows(store)
    assert windows[0]["pm_reference"] == 100_000.0
    assert windows[0]["kalshi_reference"] == 100_050.0
    report = evaluate_windows(windows, now=NOW)
    assert report["coverage"]["trigger_windows"] == 1


def test_load_windows_pre_repair_rows_stay_strike_unavailable(tmp_path):
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    # Recorded exactly as the pre-repair collector recorded: no carried
    # level on any row, and no evidence tables to fall back to. The
    # window stays honestly excluded -- never retro-filled.
    _record_full_window(quotes)
    rows = quotes.snapshots(venue="polymarket", contract_id="eth-updown-15m-1")
    assert all(row["reference_level"] is None for row in rows)
    windows = load_windows(store)
    assert windows[0]["pm_reference"] is None
    assert windows[0]["kalshi_reference"] is None
    report = evaluate_windows(windows, now=NOW)
    assert report["coverage"]["exclusions"]["strike_unavailable"] == 1
    assert report["coverage"]["trigger_windows"] == 0


def test_load_windows_survives_a_pre_repair_schema(tmp_path):
    # The snapshots table exists but predates the reference_level
    # column: the loader reads the levels as unavailable, not as an
    # error.
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    with store.connect() as conn:
        conn.execute(
            "CREATE TABLE rival_convergence_book_snapshots ("
            "venue TEXT NOT NULL, asset TEXT NOT NULL,"
            " contract_id TEXT NOT NULL, window_open TEXT NOT NULL,"
            " window_close TEXT NOT NULL, source_at TEXT NOT NULL,"
            " received_at TEXT NOT NULL, best_bid DOUBLE PRECISION,"
            " best_ask DOUBLE PRECISION, bid_size DOUBLE PRECISION,"
            " ask_size DOUBLE PRECISION, avg_bid DOUBLE PRECISION,"
            " avg_ask DOUBLE PRECISION, fee_rate DOUBLE PRECISION,"
            " book_sha256 TEXT NOT NULL,"
            " PRIMARY KEY (venue, contract_id, source_at))")
        conn.execute(
            "INSERT INTO rival_convergence_book_snapshots VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("polymarket", "ETH", "eth-updown-15m-1", OPEN.isoformat(),
             (OPEN + timedelta(minutes=15)).isoformat(),
             (OPEN + timedelta(seconds=300)).isoformat(),
             (OPEN + timedelta(seconds=300)).isoformat(),
             0.39, 0.41, 20.0, 20.0, 0.39, 0.40, 0.01, "sha"))
    windows = load_windows(store)
    assert len(windows) == 1
    assert windows[0]["pm_reference"] is None


def _record_snapshot(quotes, *, opened, contract_id):
    assert quotes.record({
        "venue": "polymarket", "asset": "ETH", "contract_id": contract_id,
        "window_open": opened, "window_close": opened + timedelta(minutes=15),
        "source_at": opened + timedelta(seconds=300),
        "received_at": opened + timedelta(seconds=300),
        "best_bid": 0.39, "best_ask": 0.41, "bid_size": 20.0,
        "ask_size": 20.0, "avg_bid": 0.39, "avg_ask": 0.40,
        "fee_rate": 0.01, "reference_level": None,
        "book_sha256": "ab" * 32,
    })


def test_load_windows_reads_only_the_registered_epoch(tmp_path):
    """Rectification 2026-11-27: the loader is bounded to the registered
    recording epoch [EPOCH_WINDOW_START, ACCRUAL_DEADLINE). Windows
    before collection went live and after the frozen deadline are not
    read; the epoch's first window (containing COLLECTION_START) is."""
    from parlay_bot.perpetuals.convergence_evaluation import (
        COLLECTION_START,
        EPOCH_WINDOW_START,
    )
    assert EPOCH_WINDOW_START == datetime(2026, 11, 19, 2, 0, tzinfo=UTC)
    assert EPOCH_WINDOW_START <= COLLECTION_START < (
        EPOCH_WINDOW_START + timedelta(minutes=15))
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    _record_snapshot(quotes, opened=datetime(2026, 11, 18, 0, 0, tzinfo=UTC),
                     contract_id="eth-pre-epoch")
    _record_snapshot(quotes, opened=EPOCH_WINDOW_START,
                     contract_id="eth-first-window")
    _record_snapshot(quotes, opened=OPEN, contract_id="eth-in-epoch")
    _record_snapshot(quotes, opened=datetime(2026, 12, 11, 0, 0, tzinfo=UTC),
                     contract_id="eth-post-deadline")
    windows = load_windows(store)
    assert [w["window_open"] for w in windows] == [EPOCH_WINDOW_START, OPEN]


def test_snapshot_eval_indexes_exist_and_serve_the_evaluator_order(tmp_path):
    """The covering indexes for the evaluators' ORDER BY exist after
    collector init, and SQLite's plan for the bounded convergence scan
    uses one instead of a temp B-tree sort over a full scan."""
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    with store.connect() as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='rival_convergence_book_snapshots'").fetchall()}
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT venue, asset, contract_id,"
            " window_open, source_at FROM rival_convergence_book_snapshots"
            " WHERE window_open>=? AND window_open<?"
            " ORDER BY asset, window_open, venue, source_at",
            ("2026-11-19T02:00:00+00:00", "2026-12-10T02:12:00+00:00"),
        ).fetchall()
    assert "idx_convergence_snapshots_eval_order" in names
    assert "idx_convergence_snapshots_venue_order" in names
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "idx_convergence_snapshots_eval_order" in plan_text
    assert "TEMP B-TREE" not in plan_text
