"""The trial table: one honest row per model, readable on a phone."""
from __future__ import annotations

from parlay_bot.discord_app import trial_board
from parlay_bot.discord_app.trial_board import TrialRow, fold, leader_line, short_name, table


def _grouped(text: str) -> list[tuple[str, list[str]]]:
    """(status, row words) per model line of a grouped phone table."""
    from parlay_bot.discord_app.trial_board import GROUP_LABELS

    label_to_status = {v: k for k, v in GROUP_LABELS.items()}
    lines = text.split("```")[1].strip().splitlines()[1:]
    out, status = [], None
    for line in lines:
        if line in label_to_status:
            status = label_to_status[line]
        else:
            out.append((status, line.split()))
    return out


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
    # Nov 11: Discord on a phone wraps past ~35 characters; no status column.
    assert max(len(line) for line in lines) <= trial_board.PHONE_WIDTH
    assert _grouped(table(rows)) == [("behind", ["Order-flow-", "99/21", "12M", "—", "-1000"])]
    assert trial_board._tiny_count(2_062) == "2.1k" and trial_board._tiny_count(140) == "140"
    assert trial_board._count(9_999) == "9,999" and trial_board._count(45_000) == "45k"


def test_the_leader_line_says_what_is_left():
    lead = TrialRow(name="Trend-only", days=12, results=96, gain=.003, ahead=7,
                    exchanges=9, stopped=0, passed=False, after_fees_bps=.4)
    assert leader_line([lead]) == (
        "Closest to passing: **Trend-only** (ahead on 7 of 9 exchanges, ahead after fees). "
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
    assert _grouped(trial_board.contest_table(rows)) == [
        ("PASSED", ["Formula", "500", "+1.0", "+1.2¢"]),
        ("behind", ["Jump", "model", "400", "-2.0", "—"])]
    assert max(len(line) for line in trial_board.contest_table(rows).splitlines()) <= 35


def test_the_right_column_is_the_share_of_calls_that_came_true():
    row = TrialRow(name="Trend-only", days=12, results=300, gain=.003, ahead=2,
                   exchanges=2, stopped=0, passed=False, after_fees_bps=None,
                   family="regime_v1", calls=200, right=113)
    assert _grouped(table([row])) == [
        ("ahead", ["Trend-only", "12/21", "300", "56%", "+3.0"])]
    few = TrialRow(name="Cross-exch", days=1, results=9, gain=.01, ahead=1, exchanges=1,
                   stopped=0, passed=False, after_fees_bps=None, calls=9, right=9)
    assert few.hit_rate is None          # 9 calls: no rate, never "100%"


def test_read_adds_each_models_hit_rate_from_the_ledger(tmp_path):
    import json
    from datetime import UTC, datetime, timedelta

    from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.storage.hot import SQLiteHotStore

    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "rates.sqlite")
    try:
        ShadowModelCompetition(store).initialize()
        with store.connect() as conn:
            conn.executescript(FAMILY_PROMOTION_SCHEMA)
            conn.execute(
                """INSERT INTO rival_family_promotion (venue,contract,quote,asset,
                     horizon_minutes,evaluated_at,selected_family,status,reason,evidence_json)
                   VALUES ('okx','BTC-okx','USDT','BTC',5,?,NULL,'held','t',?)""",
                (now.isoformat(), json.dumps({"regime_v1": _info(12, 300, .003)})))

            def call(i, p, up, *, age=timedelta(hours=1), horizon=5, asset="BTC",
                     settled=True, venue="okx"):
                issued = now - age
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                         horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                         probability_up,incumbent_up,baseline_up,training_count,
                         training_sha256,settled_at,outcome_up,model_brier,incumbent_brier,
                         baseline_brier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, f"{asset}-okx", "USDT", asset, horizon, "regime_v1",
                     (issued - timedelta(seconds=i)).isoformat(), issued.isoformat(),
                     issued.isoformat(), p, .5, .5, 0, "s" * 64,
                     issued.isoformat() if settled else None, int(up) if settled else None,
                     .2, .25, .25))

            for i in range(30):
                call(i, .6, i < 18)                      # 18 of 30 came true
            call(100, .5, True)                          # 50/50: not a call
            call(101, .7, True, settled=False)           # not graded yet
            call(102, .7, True, age=timedelta(days=8))   # older than the 7 days
            call(103, .7, True, horizon=15)              # another timeframe
            call(104, .7, True, asset="ETH")             # another coin
            # Nov 11 audit: an exchange the promotion check did not judge here
            # must not mix into Right (Day, Res and Edge are okx-only).
            for i in range(40):
                call(200 + i, .7, True, venue="gate")
        board = trial_board.read(store, asset="BTC", horizon=5, now=now)
        row = board["rows"][0]
        assert (row.family, row.calls, row.right) == ("regime_v1", 30, 18)
        assert board["venues"] == ["okx"]
        assert _grouped(table(board["rows"]))[0][1][3] == "60%"
    finally:
        store.close()


def test_reverted_venue_variants_never_show():
    """'<family>_venue' (#365) was reverted in #366: its old rows must not
    sit on the board as "day 1/21, 5 results" forever."""
    from parlay_bot.discord_app.trial_board import fold, is_retired

    assert is_retired("pooled_15m_1a10acfb6945_venue")
    assert is_retired("flow_v1_venue")
    assert not is_retired("pooled_15m_1a10acfb6945")
    info = {"targets": 5, "days": 1, "forward_gain": .017}
    rows = fold([("okx", "BTC-okx", {"pooled_5m_aaaaaaaaaaaa_venue": info,
                                      "flow_v2_venue": info,
                                      "regime_v1": {"targets": 200, "days": 4}})], [])
    assert [r.name for r in rows] == ["Trend-only"]


def test_an_edge_on_few_results_says_few_and_a_long_record_outranks_a_lucky_one():
    from parlay_bot.discord_app.trial_board import best

    lucky = TrialRow(name="Trend learn", days=3, results=41, gain=.010, ahead=1,
                     exchanges=1, stopped=0, passed=False, after_fees_bps=None)
    steady = TrialRow(name="6-year", days=8, results=703, gain=.003, ahead=1,
                      exchanges=1, stopped=0, passed=False, after_fees_bps=None)
    tiny = TrialRow(name="Fast trend", days=3, results=10, gain=.0447, ahead=1,
                    exchanges=1, stopped=0, passed=False, after_fees_bps=None)
    assert best([lucky, steady, tiny]) is steady
    assert _grouped(table([tiny]))[0] == ("early", ["Fast", "trend", "3/21", "10", "—", "few"])
