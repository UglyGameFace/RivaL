"""The trial table: one honest row per model, readable on a phone."""
from __future__ import annotations

from parlay_bot.discord_app import trial_board
from parlay_bot.discord_app.trial_board import TrialRow, fold, leader_line, short_name, table


def _info(days, targets, gain, **extra):
    return {"days": days, "targets": targets, "forward_gain": gain, **extra}


def test_every_6_year_version_folds_into_its_most_advanced_one():
    rows = fold([("okx", "BTC", {
        "pooled_5m_aaaaaaaaaaaa": _info(3, 40, .01),
        "pooled_5m_bbbbbbbbbbbb": _info(12, 600, -.002),
    })], [])
    assert len(rows) == 1 and rows[0].name == "6-year" and rows[0].days == 12
    assert short_name("pooled_15m_1a10acfb6945_venue") == "6-year/ex"
    assert short_name("regime_live_fast_v1") == "Fast trend"
    assert short_name("some_brand_new_model_family") == "some_brand_n"


def test_gain_is_weighted_by_results_and_exchanges_ahead_are_counted():
    rows = fold([
        ("okx", "BTC", {"cross_v1": _info(5, 300, .004)}),
        ("bybit", "BTC", {"cross_v1": _info(4, 100, -.004)}),
    ], [])
    row = rows[0]
    assert row.days == 5 and row.results == 300
    assert abs(row.gain - .002) < 1e-12          # (300*.004 - 100*.004) / 400
    assert (row.ahead, row.exchanges) == (1, 2)
    assert row.status == "ahead"


def test_status_order_puts_passed_first_and_cut_last():
    rows = fold([("okx", "BTC", {
        "liq_v1": _info(11, 581, -.06),
        "regime_v1": _info(21, 900, .003, qualified=True),
        "cross_v1": _info(2, 10, .02),
        "regime_wild_v1": _info(12, 120, .0001),
        "pooled_5m_cccccccccccc": _info(12, 649, -.002),
    })], [("okx", "BTC", "liq_v1")])
    assert [(r.name, r.status) for r in rows] == [
        ("Trend-only", "PASSED"), ("Wild-only", "level"), ("Cross-exch", "early"),
        ("6-year", "behind"), ("Liquidity", "cut")]
    assert leader_line(rows).startswith("✅ **Trend-only** passed every test")


def test_a_model_cut_on_only_some_exchanges_is_still_on_trial():
    rows = fold([("okx", "BTC", {"liq_v1": _info(9, 300, -.01)}),
                 ("bybit", "BTC", {"liq_v1": _info(9, 300, -.01)})],
                [("okx", "BTC", "liq_v1")])
    assert rows[0].stopped == 1 and rows[0].status == "behind"


def test_retired_models_and_malformed_evidence_are_left_out():
    rows = fold([("okx", "BTC", {
        "flow_v1": _info(9, 300, .01),
        "regime_v1": {"days": "9", "targets": 300},     # not a number: not a row
        "cross_v1": "garbage",
    }), ("bybit", "BTC", "not a dict")], [], retired=frozenset({"flow_v1"}))
    assert rows == []


def test_the_table_fits_a_phone_even_at_extremes():
    rows = [TrialRow(name="Order-flow-12", days=140, results=12_345_678, gain=-.9999,
                     ahead=0, exchanges=9, stopped=0, passed=False, after_fees_bps=None)]
    lines = table(rows).strip("`\n").splitlines()
    assert max(len(line) for line in lines) <= 38
    assert lines[1].split()[:4] == ["Order-flow-1", "99/21", "12M", "-1000"]
    assert trial_board._count(9_999) == "9,999" and trial_board._count(45_000) == "45k"


def test_the_leader_line_says_what_is_left():
    lead = TrialRow(name="Trend-only", days=12, results=96, gain=.003, ahead=7,
                    exchanges=9, stopped=0, passed=False, after_fees_bps=.4)
    assert leader_line([lead]) == (
        "Closest: **Trend-only** (ahead on 7 of 9 exchanges, ahead after fees). "
        "Needs 9 more days and 54 more results before it can be trusted.")
    done = TrialRow(name="Trend-only", days=25, results=400, gain=.003, ahead=7,
                    exchanges=9, stopped=0, passed=False, after_fees_bps=None)
    assert leader_line([done]).endswith("Waiting on the after-fees test.")
    assert leader_line([]) == "No test model is beating the main model here yet."


def test_contest_rows_keep_each_models_best_covered_source():
    rows = trial_board.contest_rows([
        {"model": "m", "label": "Jump diffusion", "contracts": 40, "improvement": .002},
        {"model": "m", "label": "Jump diffusion", "contracts": 400, "improvement": -.002},
        {"model": "w", "label": "GBM + EWMA", "contracts": 500, "improvement": .001,
         "significant": True, "trades": 200, "net_per_contract": .012},
    ])
    assert [(r["model"], r["contracts"], r["status"]) for r in rows] == [
        ("w", 500, "PASSED"), ("m", 400, "behind")]
    lines = trial_board.contest_table(rows).strip("`\n").splitlines()
    assert lines[1].split() == ["Formula", "500", "+1.0", "+1.2¢", "PASSED"]
    assert lines[2].split() == ["Jump", "model", "400", "-2.0", "—", "behind"]
