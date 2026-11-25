"""Live grading view (Felix, 2026-11-24): interim, read-only grades.

The blocks must report the frozen evaluators' own counters and leg
formulas applied to accrued data — never a verdict of their own — and
each block must fail independently to read_unavailable.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals import live_grading
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    hot = SQLiteHotStore(tmp_path / "grading.sqlite")
    market = PerpetualMarketStore(hot)
    market.initialize()
    ForwardAccuracyMonitor(market).initialize()
    yield hot
    hot.close()


def _settled(store, *, issued, p, baseline, up, horizon=5):
    settled = issued + timedelta(minutes=horizon)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit(
                 venue,contract,quote,asset,horizon_minutes,model_version,
                 reference_opened_at,reference_close,issued_at,target_opened_at,
                 probability_up,baseline_up,training_evaluated,settled_at,
                 outcome_up,realized_close,model_brier,baseline_brier)
               VALUES ('gate','BTC_USDT','USDT','BTC',?,?,?,100.0,?,?,?,?,100,?,?,100.0,?,?)""",
            (horizon, MODEL, issued.isoformat(), issued.isoformat(),
             settled.isoformat(), p, baseline, settled.isoformat(), up,
             (p - up) ** 2, (baseline - up) ** 2))


def test_live_model_block_grades_against_the_issuance_baseline(store):
    # Three days of rows where the model (0.7) beats the baseline (0.5).
    for day in range(3):
        for k in range(4):
            _settled(store, issued=NOW - timedelta(days=day, hours=k),
                     p=0.7, baseline=0.5, up=1)
    block = live_grading._live_model_block(store, NOW)
    week = block["last_7d"]
    assert week["settled"] == 12
    assert week["delta_log_loss"] < 0
    assert week["delta_brier"] == pytest.approx(0.09 - 0.25)
    assert week["delta_log_loss_ci95"]["upper"] < 0
    assert week["accuracy"] == 1.0
    assert week["baseline_accuracy"] == 0.0
    assert set(block["per_horizon_7d"]) == {"5"}


def test_live_model_block_empty_is_zero_not_error(store):
    block = live_grading._live_model_block(store, NOW)
    assert block["last_7d"]["settled"] == 0
    assert block["last_7d"]["delta_log_loss"] is None


def _fv_report(delta_brier):
    score = {
        "observations": 100, "windows": 90,
        "delta_log_loss": -0.01,
        "delta_log_loss_ci95": {"lower": -0.02, "upper": -0.001, "days": 5},
        "delta_brier": delta_brier,
        "delta_brier_ci95": {"lower": -0.004, "upper": -0.0005, "days": 5},
        "chronological_halves": {
            "first_delta_log_loss": -0.012, "second_delta_log_loss": -0.008,
            "first_call_mean_net": 0.01, "second_call_mean_net": 0.02,
            "first_n": 50, "second_n": 50},
        "calls": {"n": 30, "mean_net": 0.015, "win_rate": 0.6,
                  "breakeven_win_rate": 0.55},
        "trigger_rate": 0.3, "near_strike_share": 0.1,
    }
    venue_block = {
        "status": "ACCRUAL", "verdict": None,
        "floors": {
            "recording_days": {"have": 5, "need": 7},
            "evaluated_windows": {"have": 90, "need": 2500},
            "triggered_calls": {"have": 30, "need": 250},
            "extension_used": False},
        "primary": score,
        "per_asset": {"BTC": score},
        "coverage": {"windows_seen": 95, "window_exclusions": {},
                     "observation_exclusions": {}},
    }
    return {"venues": {"polymarket": venue_block}}


def test_fair_value_block_shows_frozen_and_corrected_brier_rulers():
    # -0.0028 beats the corrected ruler (CI entirely < 0) but not the
    # frozen -0.005 bar: the two leg states must differ, honestly.
    block = live_grading._fair_value_block("polymarket", _fv_report(-0.0028))
    assert block["status"] == "ACCRUAL" and block["verdict"] is None
    assert block["legs"]["paired_brier_frozen"]["passing_now"] is False
    assert block["legs"]["paired_brier_corrected"]["passing_now"] is True
    assert block["legs"]["paired_log_loss"]["passing_now"] is True
    assert block["floors"]["triggered_calls"] == {"have": 30, "need": 250}
    assert block["per_asset"]["BTC"]["calls"] == 30


def test_convergence_block_reports_floors_and_interim_legs():
    report = {
        "status": "ACCRUAL", "verdict": None,
        "coverage": {"windows_seen": 500, "trigger_windows": 41,
                     "trigger_distinct_days": 4, "exclusions": {},
                     "fee_verified_rate": 1.0},
        "cuts": {
            "pooled": {"n": 41, "distinct_days": 4,
                       "mean_net_per_contract": -0.01,
                       "win_rate": 0.4, "day_block_interval": None,
                       "chronological_halves": {
                           "first_mean": 0.02, "second_mean": -0.04,
                           "first_n": 20, "second_n": 21}},
            "per_asset": {"BTC": {"n": 41, "mean_net_per_contract": -0.01}},
        },
    }
    block = live_grading._convergence_block(report, NOW)
    assert block["floors"]["trigger_windows"] == {"have": 41, "need": 300}
    assert block["legs"]["mean_net"]["passing_now"] is False
    assert block["legs"]["both_halves_net"]["passing_now"] is False
    assert block["days_to_deadline"] > 0
    assert "fee" in block["note"]


def test_read_live_grading_degrades_per_block_on_an_empty_store(store):
    section = live_grading.read_live_grading(store, NOW)
    assert section["schema"] == "rival-live-grading-v1"
    ids = [t["id"] for t in section["tests"]]
    assert "convergence" in ids and "n3" in ids
    n3 = next(t for t in section["tests"] if t["id"] == "n3")
    assert n3["status"] == "external_recorder"
    assert n3["live_counters"] is None
    assert section["live_model"]["last_7d"]["settled"] == 0


def test_candidates_block_names_blockers(store):
    from parlay_bot.perpetuals import replay_generation as gen

    at = "2026-11-20T17:00:00+00:00"
    gen.record_results(store, [
        {"candidate_id": "llm-mask-btc-trend", "screen": "G-D",
         "factory": "lab", "display": "AI-suggested mask · BTC",
         "status": "NOT_MEASURABLE", "verdict": None, "graded_rows": 0,
         "calls": 0, "days": 0, "evaluated_at": at,
         "detail": {"cause": "exchange_features_not_recorded"}},
        {"candidate_id": "gen-book-all", "screen": "G-A",
         "factory": "lab", "display": "Book model · all book signals",
         "status": "EVALUATED", "verdict": "KILL", "graded_rows": 2100,
         "calls": 2000, "days": 14, "evaluated_at": at, "detail": {}},
    ])
    block = live_grading.read_live_grading(store, NOW)["candidates"]
    assert block["status"] == "recorded"
    assert block["snapshot_at"] == at
    assert block["counts"] == {"total": 2, "passed": 0, "killed": 1,
                               "not_measurable": 1, "data_blocked": 0,
                               "evaluated": 0}
    by_id = {c["id"]: c for c in block["candidates"]}
    assert "no grader reads them yet" in by_id["llm-mask-btc-trend"]["blocker"]
    assert by_id["gen-book-all"]["blocker"] is None
