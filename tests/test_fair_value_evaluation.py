"""Settlement-faithful fair-value evaluator: frozen criteria, fail-closed.

The verdict constructions are synthetic by design: the index series is
a deterministic alternating walk (so its trailing sigma is known
exactly), and each window's strike is solved from the module's own
projected average so the fair value lands on a target -- the fair-value
MATH is pinned separately below against hand-computed values, so the
verdict tests only exercise the verdict logic.
"""
import math
from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import pytest

from parlay_bot.perpetuals import fair_value_evaluation as fve
from parlay_bot.perpetuals.convergence_quotes import ConvergenceQuotes
from parlay_bot.perpetuals.fair_value_evaluation import (
    call_decision,
    evaluate_fair_value,
    fair_value,
    load_windows,
    projected_settlement_average,
    trailing_sigma,
)
from parlay_bot.perpetuals.kalshi_updown import KalshiUpDown
from parlay_bot.perpetuals.settlement_index import (
    IndexTick,
    SettlementIndexRecorder,
)
from parlay_bot.storage.hot import SQLiteHotStore

ND = NormalDist()
OPEN = datetime(2026, 11, 21, 12, 0, 0, tzinfo=UTC)


def _series(anchor, *, vol=0.001, step=5, hours_before=2, hours_after=26):
    """Alternating walk: every log increment is +/- vol*sqrt(step)."""
    start = anchor - timedelta(hours=hours_before)
    ticks, price, sign = [], 100.0, 1
    at = start
    end = anchor + timedelta(hours=hours_after)
    a = vol * math.sqrt(step)
    while at <= end:
        ticks.append((at, price))
        price *= math.exp(sign * a)
        sign = -sign
        at += timedelta(seconds=step)
    return ticks


def _window(ticks, *, venue="polymarket", open_at, target_fair=0.85, mid=0.60,
            spread=0.04, label=1, fee_rate=0.0, asset="BTC", offsets=(300, 600, 840)):
    at = open_at + timedelta(seconds=300)
    mu, level = projected_settlement_average(ticks, window_open=open_at, at=at)
    sigma = trailing_sigma(ticks, at=at)
    strike = mu - ND.inv_cdf(target_fair) * level * sigma * math.sqrt(560.0)
    snaps = [{
        "source_at": (open_at + timedelta(seconds=off)).isoformat(),
        "avg_bid": mid - spread / 2, "avg_ask": mid + spread / 2,
        "fee_rate": fee_rate, "book_sha256": "x" * 64,
    } for off in offsets]
    return {
        "venue": venue, "asset": asset,
        "contract_id": f"{venue}-{open_at.isoformat()}",
        "window_open": open_at, "window_close": open_at + timedelta(minutes=15),
        "strike": strike, "settlement": label, "snapshots": snaps,
        "index_ticks": ticks, "recording_days": 7,
        "recording_first_full_start": datetime(2026, 11, 21, tzinfo=UTC),
        "recording_last_tick": open_at + timedelta(days=2),
    }


def _sample(*, loud_days=4, per_day=50, quiet=False, label=1):
    windows = []
    base = datetime(2026, 11, 21, tzinfo=UTC)
    for d in range(loud_days):
        day = base + timedelta(days=d)
        ticks = _series(day)
        for i in range(per_day):
            open_at = day + timedelta(hours=1, minutes=15 * i)
            if quiet and i != 0:
                windows.append(_window(
                    ticks, open_at=open_at, target_fair=0.502, mid=0.5015,
                    label=label))
            else:
                windows.append(_window(
                    ticks, open_at=open_at, target_fair=0.85, mid=0.60,
                    label=label))
    return windows


@pytest.fixture
def small_floors(monkeypatch):
    monkeypatch.setattr(fve, "MIN_RECORDING_DAYS", 4)
    monkeypatch.setattr(fve, "MIN_EVALUATED_WINDOWS", 10)
    monkeypatch.setattr(fve, "MIN_TRIGGERED_CALLS", 2)


# ---------------------------------------------------------------------------
# Fair-value mathematics, pinned against hand computation.
# ---------------------------------------------------------------------------

def test_flat_series_prices_a_strike_at_the_level_as_a_coin_flip():
    ticks = [(OPEN - timedelta(seconds=s), 100.0) for s in range(3600, -900, -5)]
    fv = fair_value(ticks, window_open=OPEN, strike=100.0,
                    at=OPEN + timedelta(seconds=300))
    assert fv is not None
    assert fv["mu"] == pytest.approx(100.0)
    assert fv["fair_value"] == pytest.approx(0.5)


def test_remaining_std_outside_the_final_minute():
    ticks = _series(OPEN)
    at = OPEN + timedelta(seconds=300)
    mu, level = projected_settlement_average(ticks, window_open=OPEN, at=at)
    sigma = trailing_sigma(ticks, at=at)
    assert sigma == pytest.approx(0.001, rel=1e-6)
    strike = mu - level * sigma * math.sqrt(560.0)  # exactly one std below mu
    fv = fair_value(ticks, window_open=OPEN, strike=strike, at=at)
    assert fv["remaining_std"] == pytest.approx(level * sigma * math.sqrt(560.0))
    assert fv["fair_value"] == pytest.approx(ND.cdf(1.0), abs=1e-9)


def test_remaining_std_inside_the_final_minute_uses_the_remainder_term():
    ticks = _series(OPEN)
    at = OPEN + timedelta(seconds=870)
    mu, level = projected_settlement_average(ticks, window_open=OPEN, at=at)
    sigma = trailing_sigma(ticks, at=at)
    expected = level * sigma * 30.0 ** 1.5 / (math.sqrt(3.0) * 60.0)
    strike = mu - expected
    fv = fair_value(ticks, window_open=OPEN, strike=strike, at=at)
    assert fv["remaining_std"] == pytest.approx(expected)
    assert fv["fair_value"] == pytest.approx(ND.cdf(1.0), abs=1e-9)


def test_projected_average_integrates_the_printed_final_minute():
    ticks = []
    for s in range(800, 900):
        at = OPEN + timedelta(seconds=s)
        value = 100.0 if s <= 840 else 100.0 + (s - 840)
        ticks.append((at, value))
    at = OPEN + timedelta(seconds=870)
    mu, level = projected_settlement_average(ticks, window_open=OPEN, at=at)
    # Printed [840, 870): values 100..129 for one second each = 3435;
    # the unprinted 30s are projected at the current level (130).
    assert level == pytest.approx(130.0)
    assert mu == pytest.approx((3435 + 130 * 30) / 60)


def test_trailing_sigma_fails_closed_on_gaps_and_staleness():
    ticks = _series(OPEN)
    at = OPEN + timedelta(seconds=300)
    gapped = [t for t in ticks
              if not (at - timedelta(seconds=600) < t[0] < at - timedelta(seconds=380))]
    assert trailing_sigma(gapped, at=at) is None
    stale = [t for t in ticks if t[0] < at - timedelta(seconds=200)]
    assert trailing_sigma(stale, at=at) is None
    assert trailing_sigma([], at=at) is None


def test_call_rule_demands_fair_value_clear_the_buffer():
    side, cost, _ = call_decision(
        fair=0.70, avg_ask=0.60, avg_bid=0.58, fee_up=0.0, fee_down=0.0,
        buffer=0.035)
    assert side == "up" and cost == pytest.approx(0.60)
    side, _, _ = call_decision(
        fair=0.61, avg_ask=0.60, avg_bid=0.58, fee_up=0.0, fee_down=0.0,
        buffer=0.035)
    assert side is None  # an edge inside the buffer is a NO CALL
    side, cost, _ = call_decision(
        fair=0.30, avg_ask=0.42, avg_bid=0.40, fee_up=0.0, fee_down=0.0,
        buffer=0.035)
    assert side == "down" and cost == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# The accrual guard and the verdicts.
# ---------------------------------------------------------------------------

def test_accrual_guard_refuses_a_verdict_below_the_frozen_floors():
    # A small sample with a huge apparent edge still cannot print a verdict.
    report = evaluate_fair_value(_sample(loud_days=1, per_day=8),
                                 now=datetime(2026, 11, 26, tzinfo=UTC))
    pm = report["venues"]["polymarket"]
    assert pm["status"] == "ACCRUAL"
    assert pm["verdict"] is None
    assert pm["floors"]["evaluated_windows"]["need"] == 2500
    assert pm["primary"]["delta_brier"] < -0.005  # the edge is real, and moot


def test_pass_when_all_four_legs_hold(small_floors):
    report = evaluate_fair_value(_sample(), now=datetime(2026, 11, 26, tzinfo=UTC))
    pm = report["venues"]["polymarket"]
    assert pm["status"] == "EVALUATED" and pm["verdict"] == "PASS"
    assert all(pm["legs"].values())
    # The other venue has no sample at all: accrual, never a borrowed verdict.
    assert report["venues"]["kalshi"]["verdict"] is None


def test_kill_when_the_labels_disagree(small_floors):
    report = evaluate_fair_value(_sample(label=0),
                                 now=datetime(2026, 11, 26, tzinfo=UTC))
    pm = report["venues"]["polymarket"]
    assert pm["status"] == "EVALUATED" and pm["verdict"] == "KILL"
    assert not any(pm["legs"].values())


def test_borderline_extends_once_then_the_extended_sample_decides(small_floors):
    windows = _sample(quiet=True)
    report = evaluate_fair_value(windows, now=datetime(2026, 11, 26, tzinfo=UTC))
    pm = report["venues"]["polymarket"]
    assert -0.005 <= pm["primary"]["delta_brier"] < -0.002
    assert pm["status"] == "BORDERLINE_EXTENSION" and pm["verdict"] is None

    extended = evaluate_fair_value(
        windows, now=datetime(2026, 11, 26, tzinfo=UTC), extension_used=True)
    assert extended["venues"]["polymarket"]["status"] == "ACCRUAL"  # 7 < 4+7 days
    for window in windows:
        window["recording_days"] = 11
    decided = evaluate_fair_value(
        windows, now=datetime(2026, 11, 26, tzinfo=UTC), extension_used=True)
    pm = decided["venues"]["polymarket"]
    assert pm["status"] == "EVALUATED" and pm["verdict"] == "KILL"


def test_frozen_criteria_match_the_preregistration():
    assert fve.WINDOW_SECONDS == 900
    assert fve.PRIMARY_OFFSETS == (300, 600)
    assert fve.SECONDARY_OFFSET == 840
    assert fve.CHECKPOINT_TOLERANCE_SECONDS == 45
    assert fve.VOL_LOOKBACK_SECONDS == 3600
    assert fve.VOL_GAP_SECONDS == 120
    assert fve.SAFETY_MARGIN == 0.015
    assert fve.BUFFER_FLOOR == 0.025
    assert fve.MIN_RECORDING_DAYS == 7
    assert fve.MIN_EVALUATED_WINDOWS == 2500
    assert fve.MIN_TRIGGERED_CALLS == 250
    assert fve.PASS_BRIER == -0.005
    assert (fve.BORDERLINE_BRIER_LOW, fve.BORDERLINE_BRIER_HIGH) == (-0.005, -0.002)
    assert fve.EXTENSION_DAYS == 7


# ---------------------------------------------------------------------------
# Fail-closed exclusions, checkpoint separation, per-venue isolation.
# ---------------------------------------------------------------------------

def _bare_window(**overrides):
    ticks = _series(OPEN)
    window = _window(ticks, open_at=OPEN)
    window.update(overrides)
    return window


def test_each_missing_input_is_excluded_and_counted():
    cases = [
        ({"strike": None}, "strike_unavailable"),
        ({"settlement": None}, "settlement_unavailable"),
        ({"index_ticks": []}, "index_missing"),
        ({"recording_first_full_start": OPEN + timedelta(days=1)},
         "outside_recording"),
    ]
    for overrides, reason in cases:
        report = evaluate_fair_value([_bare_window(**overrides)],
                                     now=datetime(2026, 11, 26, tzinfo=UTC))
        coverage = report["venues"]["polymarket"]["coverage"]
        assert coverage["window_exclusions"][reason] == 1, reason
        assert coverage["evaluated_windows"] == 0


def test_observation_level_exclusions():
    # Snapshots only at t=840: the primary checkpoints are missing, the
    # secondary sub-test still evaluates -- and never feeds the verdict.
    window = _bare_window()
    window["snapshots"] = [s for s in window["snapshots"]
                           if s["source_at"] == (OPEN + timedelta(seconds=840)).isoformat()]
    report = evaluate_fair_value([window], now=datetime(2026, 11, 26, tzinfo=UTC))
    pm = report["venues"]["polymarket"]
    assert pm["coverage"]["observation_exclusions"]["missing_snapshot"] == 2
    assert pm["coverage"]["primary_observations"] == 0
    assert pm["coverage"]["secondary_observations"] == 1
    assert pm["secondary_t840"]["observations"] == 1

    thin = _bare_window()
    for snap in thin["snapshots"]:
        snap["avg_ask"] = None
    report = evaluate_fair_value([thin], now=datetime(2026, 11, 26, tzinfo=UTC))
    assert report["venues"]["polymarket"]["coverage"][
        "observation_exclusions"]["unfillable_leg"] == 3

    unverified = _bare_window()
    for snap in unverified["snapshots"]:
        snap["fee_rate"] = None
    report = evaluate_fair_value([unverified], now=datetime(2026, 11, 26, tzinfo=UTC))
    assert report["venues"]["polymarket"]["coverage"][
        "observation_exclusions"]["fee_unverified"] == 3

    # Kalshi snapshots store fee_rate NULL by design: the fee comes from
    # kalshi_fee() and the window evaluates.
    kalshi = _bare_window(venue="kalshi")
    for snap in kalshi["snapshots"]:
        snap["fee_rate"] = None
    report = evaluate_fair_value([kalshi], now=datetime(2026, 11, 26, tzinfo=UTC))
    assert report["venues"]["kalshi"]["coverage"]["primary_observations"] == 2


def test_index_gap_at_a_checkpoint_is_counted_not_imputed():
    ticks = _series(OPEN)
    at = OPEN + timedelta(seconds=300)
    gapped = [t for t in ticks
              if not (at - timedelta(seconds=600) < t[0] < at - timedelta(seconds=380))]
    window = _bare_window(index_ticks=gapped)
    report = evaluate_fair_value([window], now=datetime(2026, 11, 26, tzinfo=UTC))
    pm = report["venues"]["polymarket"]
    assert pm["coverage"]["observation_exclusions"]["index_gap"] >= 1
    assert pm["coverage"]["evaluated_windows"] == 0


# ---------------------------------------------------------------------------
# Loader: store rows -> evaluator windows (SELECT-only chain).
# ---------------------------------------------------------------------------

def test_loader_joins_snapshots_strikes_settlements_and_index(tmp_path):
    store = SQLiteHotStore(tmp_path / "fv.sqlite")
    quotes = ConvergenceQuotes(store)
    opened = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    close = opened + timedelta(minutes=15)
    for off in (300, 600):
        assert quotes.record({
            "venue": "polymarket", "asset": "BTC", "contract_id": "btc-updown-15m",
            "window_open": opened, "window_close": close,
            "source_at": opened + timedelta(seconds=off),
            "received_at": opened + timedelta(seconds=off),
            "best_bid": 0.59, "best_ask": 0.61, "bid_size": 50.0,
            "ask_size": 50.0, "avg_bid": 0.595, "avg_ask": 0.615,
            "fee_rate": 0.07, "reference_level": 67234.5,
            "book_sha256": "cd" * 32,
        })
    KalshiUpDown(store, None).initialize()
    assert quotes.record({
        "venue": "kalshi", "asset": "BTC", "contract_id": "KXBTC15M-TEST",
        "window_open": opened, "window_close": close,
        "source_at": opened + timedelta(seconds=300),
        "received_at": opened + timedelta(seconds=300),
        "best_bid": 0.59, "best_ask": 0.61, "bid_size": 50.0,
        "ask_size": 50.0, "avg_bid": 0.595, "avg_ask": 0.615,
        "fee_rate": None, "reference_level": None,
        "book_sha256": "cd" * 32,
    })
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_kalshi_updown (ticker, asset, opens_at, closes_at,"
            " open_reference, issued_at, result_yes) VALUES (?,?,?,?,?,?,?)",
            ("KXBTC15M-TEST", "BTC", opened.isoformat(), close.isoformat(),
             67230.0, opened.isoformat(), 1))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rival_polymarket_ai_outcomes ("
            " slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,"
            " direction TEXT NOT NULL, resolved_at TEXT NOT NULL,"
            " observed_at TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,"
            " receipt_json TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
            ("btc-updown-15m", "cond-1", "UP", close.isoformat(),
             close.isoformat(), "ef" * 32, "{}"))
    recorder = SettlementIndexRecorder(store, throttle_seconds=1)
    base = opened - timedelta(minutes=30)
    for i in range(40):
        at = base + timedelta(seconds=i * 10)
        recorder.note(IndexTick(
            venue="polymarket", asset="BTC", source_at=at, received_at=at,
            value=67200.0 + i, avg_60s=None, receipt_sha256="ab" * 32))
        recorder.flush()  # the bot loop flushes on its cadence, as here

    windows = load_windows(store)
    pm = next(w for w in windows if w["venue"] == "polymarket")
    assert pm["strike"] == pytest.approx(67234.5)  # snapshot-carried level
    assert pm["settlement"] == 1
    assert len(pm["snapshots"]) == 2
    assert len(pm["index_ticks"]) == 40
    assert pm["recording_days"] >= 1
    kalshi = next(w for w in windows if w["venue"] == "kalshi")
    assert kalshi["strike"] == pytest.approx(67230.0)  # open_reference join
    assert kalshi["settlement"] == 1


def test_loader_reads_only_the_collection_epoch(tmp_path):
    """Rectification 2026-11-27: snapshots from before the shared
    collection epoch (EPOCH_WINDOW_START) are not read; in-epoch
    windows load exactly as before."""
    store = SQLiteHotStore(tmp_path / "fv.sqlite")
    quotes = ConvergenceQuotes(store)
    opened = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    for when, contract in (
            (datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC), "btc-pre-epoch"),
            (opened, "btc-in-epoch")):
        assert quotes.record({
            "venue": "polymarket", "asset": "BTC", "contract_id": contract,
            "window_open": when, "window_close": when + timedelta(minutes=15),
            "source_at": when + timedelta(seconds=300),
            "received_at": when + timedelta(seconds=300),
            "best_bid": 0.59, "best_ask": 0.61, "bid_size": 50.0,
            "ask_size": 50.0, "avg_bid": 0.595, "avg_ask": 0.615,
            "fee_rate": 0.07, "reference_level": 67234.5,
            "book_sha256": "cd" * 32,
        })
    windows = load_windows(store)
    assert [w["contract_id"] for w in windows] == ["btc-in-epoch"]
