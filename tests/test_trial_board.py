"""The trial table: one honest row per model, readable on a phone."""
from __future__ import annotations

from parlay_bot.discord_app import trial_board
from parlay_bot.discord_app.trial_board import TrialRow, fold, leader_line, short_name, table


def _grouped(text: str) -> list[tuple[str, list[str]]]:
    """(status, row words) per model line of a grouped phone table."""
    from parlay_bot.discord_app.trial_board import CONTEST_GROUP_LABELS, GROUP_LABELS

    label_to_status = {v: k for k, v in GROUP_LABELS.items()}
    label_to_status.update({v: k for k, v in CONTEST_GROUP_LABELS.items()})
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
        "Closest on current evidence: **Trend-only** (ahead on 7 of 9 exchanges, "
        "positive aggregate after-fee comparison). Still needs 9 more days and 54 more "
        "results; then all promotion checks must pass.")
    done = TrialRow(name="Trend-only", days=25, results=400, gain=.003, ahead=7,
                    exchanges=9, stopped=0, passed=False, after_fees_bps=None)
    assert leader_line([done]).endswith("Still requires the complete promotion checks.")
    assert "after-fee comparison unavailable" in leader_line([done])
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


def test_read_adds_each_models_hit_rate_from_the_ledger(tmp_path, monkeypatch):
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
        # The hit-rate wiring is what is on trial here; regime_v1's issuance
        # switch is off since the pivot, so open it to keep it a contender.
        from parlay_bot.perpetuals import forward_audit

        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V1_CHALLENGERS", True)
        board = trial_board.read(store, asset="BTC", horizon=5, now=now)
        row = board["rows"][0]
        assert (row.family, row.calls, row.right) == ("regime_v1", 30, 18)
        assert board["venues"] == ["okx"]
        assert _grouped(table(board["rows"]))[0][1][3] == "60%"
    finally:
        store.close()


def test_hit_rates_recompute_at_most_once_per_warm_cycle(tmp_path, monkeypatch):
    """The board warm runs every 30 minutes; the shared-read TTL must span
    it, or every warm recomputes the grouped 7-day aggregate for all 12
    coin x timeframe keys (the slow-op burst of 2026-11-21)."""
    from parlay_bot.discord_app import trial_board
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "rates-cache.sqlite")
    try:
        calls = []

        def stub(store_, *, asset, horizon, now=None, venues=None):
            calls.append((asset, horizon))
            return {"regime_v1": (30, 18)}

        monkeypatch.setattr(trial_board, "_hit_rates_uncached", stub)
        first = trial_board.hit_rates(store, asset="BTC", horizon=5)
        second = trial_board.hit_rates(store, asset="BTC", horizon=5)
        assert first == second == {"regime_v1": (30, 18)}
        assert calls == [("BTC", 5)]              # second read is a cache hit
        trial_board.hit_rates(store, asset="ETH", horizon=5)
        assert calls == [("BTC", 5), ("ETH", 5)]   # a new key computes once
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


def test_retired_older_pooled_generation_never_hides_active_successor():
    old = "pooled_5m_aaaaaaaaaaaa"
    next_version = "pooled_5m_bbbbbbbbbbbb"
    rows = fold([("okx", "BTC", {
        old: _info(21, 800, -.004),
        next_version: _info(3, 180, .001),
    })], [("okx", "BTC", old)])
    assert len(rows) == 1
    assert rows[0].name == "6-year"
    assert rows[0].family == next_version
    assert rows[0].status == "ahead"


def test_qualified_generation_precedes_unqualified_older_display_name():
    rows = fold([("okx", "BTC", {
        "pooled_5m_cccccccccccc": _info(21, 900, -.002),
        "pooled_5m_dddddddddddd": _info(21, 300, .007, qualified=True),
    })], [])
    assert len(rows) == 1
    assert rows[0].family == "pooled_5m_dddddddddddd"
    assert rows[0].status == "PASSED"


def test_trial_board_excludes_fully_stopped_rows_from_active_counts(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime

    from parlay_bot.perpetuals.early_stop import SCHEMA
    from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.storage.hot import SQLiteHotStore

    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "trial_active_truth.sqlite")
    try:
        ShadowModelCompetition(store).initialize()
        with store.connect() as conn:
            conn.executescript(FAMILY_PROMOTION_SCHEMA)
            conn.execute(SCHEMA)
            for asset, evidence in (
                ("BTC", {
                    "gaussian_nb_v1": _info(17, 800, -.006),
                    "regime_live_v1": _info(4, 90, .001),
                }),
                ("ETH", {"knn21_v1": _info(17, 800, -.007)}),
            ):
                contract = asset + "-okx"
                conn.execute(
                    """INSERT INTO rival_family_promotion
                       (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                        selected_family,status,reason,evidence_json)
                       VALUES (?,?,?,?,?,?,NULL,'held','test',?)""",
                    ("okx", contract, "USDT", asset, 5, now.isoformat(),
                     json.dumps(evidence)),
                )
            for asset, family in (("BTC", "gaussian_nb_v1"),
                                  ("ETH", "knn21_v1")):
                conn.execute(
                    """INSERT INTO rival_family_early_stop
                       (venue,contract,quote,horizon_minutes,family,
                        stopped_at,rule_version,targets,days,model_brier,
                        incumbent_brier,loss,loss_lower)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("okx", asset + "-okx", "USDT", 5, family,
                     now.isoformat(), "paired-loss-z258-v1",
                     800, 17, .28, .22, .06, .04),
                )
        # Both families' issuance switches are off since the pivot; open
        # them so this test exercises the early-stop cut, not the
        # issuance-off split.
        from parlay_bot.perpetuals import forward_audit

        monkeypatch.setattr(forward_audit, "ISSUE_COMPETITION_CHALLENGERS", True)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_V1_CHALLENGERS", True)
        board = trial_board.read(store, asset="BTC", horizon=5, now=now)
        assert board["trials"] == 1
        assert board["passed"] == 0
        assert board["cut_here"] == 1
        assert board["stops"] == 2
        assert [r.family for r in board["rows"]] == ["regime_live_v1"]
        from parlay_bot.discord_app.home import _trials_sections

        section = _trials_sections({"trials": board})
        assert "1 active trial comparisons" in section["how_to_read"]
        assert "2 early-stop records" in section["how_to_read"]
        assert "1 retired or early-stopped design(s) hidden" in section["how_to_read"]
    finally:
        store.close()


def test_issuance_off_follows_the_forward_audit_switches(monkeypatch):
    """The board's live-contender test reads the same ISSUE_* switches the
    issuer obeys, so a family returns to the board if a future decision
    re-enables it — and an unknown family is never hidden."""
    from parlay_bot.discord_app.trial_board import issuance_off
    from parlay_bot.perpetuals import forward_audit

    # Trials resumed 2026-11-25: every switch the 2026-11-19 stop
    # (PR #704) turned off is on again at the shipped defaults.
    assert not issuance_off("gaussian_nb_v1")
    assert not issuance_off("knn21_v1")
    assert not issuance_off("pooled_5m_1273de73aa15")
    assert not issuance_off("lab_5m_24a17f9781")
    assert not issuance_off("cross_all_v1")
    assert not issuance_off("calib_incumbent_v1")
    assert not issuance_off("some_future_family_v1")
    # Families retired before the stop stay off on their own records.
    assert issuance_off("flow_v1")
    assert issuance_off("liq_v1")
    assert issuance_off("regime_v2")
    monkeypatch.setattr(forward_audit, "ISSUE_COMPETITION_CHALLENGERS", False)
    assert issuance_off("gaussian_nb_v1")
    assert not issuance_off("pooled_5m_1273de73aa15")   # its own switch still on


def _board_with_evidence(tmp_path, evidence, name="board.sqlite"):
    """A store whose fresh promotion evidence holds exactly ``evidence``."""
    import json
    from datetime import UTC, datetime

    from parlay_bot.perpetuals.early_stop import SCHEMA
    from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.storage.hot import SQLiteHotStore

    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / name)
    ShadowModelCompetition(store).initialize()
    with store.connect() as conn:
        conn.executescript(FAMILY_PROMOTION_SCHEMA)
        conn.execute(SCHEMA)
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                selected_family,status,reason,evidence_json)
               VALUES ('okx','BTC-okx','USDT','BTC',5,?,NULL,'held','test',?)""",
            (now.isoformat(), json.dumps(evidence)),
        )
    return store, now


def test_switch_off_families_leave_the_board_but_stay_in_the_full_record(tmp_path, monkeypatch):
    """The owner's press-test (2026-11-20): the Models leaderboard paraded
    families whose issuance PR #704 stopped as if they were live
    contenders. They must leave the leaderboard, land in ONE summary
    count, and stay fully visible in Full evidence. (Trials resumed
    2026-11-25, so this scenario pins the two families' switches off
    in-test; the split it exercises is the same.)"""
    from parlay_bot.discord_app.home import _trials_sections
    from parlay_bot.perpetuals import forward_audit

    monkeypatch.setattr(forward_audit, "ISSUE_COMPETITION_CHALLENGERS", False)
    monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V1_CHALLENGERS", False)

    store, now = _board_with_evidence(tmp_path, {
        "gaussian_nb_v1": _info(20, 4938, -.006),
        "regime_v1": _info(12, 721, .003),
        "future_v1": _info(9, 300, .001),
    })
    try:
        board = trial_board.read(store, asset="BTC", horizon=5, now=now)
        assert [r.family for r in board["rows"]] == ["future_v1"]
        assert {r.family for r in board["all_rows"]} == {
            "gaussian_nb_v1", "regime_v1", "future_v1"}
        assert board["stopped_here"] == 2
        assert board["trials"] == 1 and board["trials_all"] == 3

        section = _trials_sections({"trials": board})
        assert section["name"] == "🧪 Test models here (1)"
        assert "Naive Bayes" not in section["table"]
        assert "Trend-only" not in section["table"]
        assert ("2 retired or early-stopped design(s) hidden from this "
                "active table — full record in Full evidence."
                in section["how_to_read"])
        assert "1 active trial comparisons" in section["how_to_read"]

        full = _trials_sections({"trials": board}, full=True)
        assert full["name"] == "🧪 Test models here (3)"
        assert "Naive Bayes" in full["table"]
        assert "Trend-only" in full["table"]
        assert "hidden" not in full["how_to_read"]
        assert "3 active trial comparisons" in full["how_to_read"]
    finally:
        store.close()


def test_board_with_no_live_contenders_says_so_and_counts_the_hidden(tmp_path, monkeypatch):
    """The production case after PR #704: every family on the board is
    retired, so the section says no comparisons are active and counts
    the hidden record instead of listing dead rows. The Full page used
    to keep listing the frozen rows as contenders; the owner ordered
    the board taken down until trials resume (2026-11-25), so with no
    live trial it now shows the paused state instead. (Trials resumed
    later that day, so this scenario pins the families' switches off
    in-test; the paused state still governs any market with no live
    trial.)"""
    from parlay_bot.discord_app.home import _trials_sections
    from parlay_bot.perpetuals import forward_audit

    monkeypatch.setattr(forward_audit, "ISSUE_COMPETITION_CHALLENGERS", False)
    monkeypatch.setattr(forward_audit, "ISSUE_POOLED_CHALLENGERS", False)

    store, now = _board_with_evidence(tmp_path, {
        "gaussian_nb_v1": _info(20, 4938, -.006),
        "pooled_5m_1273de73aa15": _info(16, 4097, .003),
    }, name="all_stopped.sqlite")
    try:
        board = trial_board.read(store, asset="BTC", horizon=5, now=now)
        assert board["rows"] == []
        assert board["stopped_here"] == 2
        assert board["trials"] == 0 and board["trials_all"] == 2

        section = _trials_sections({"trials": board})
        assert section["count"] == 0
        assert section["leader"].startswith(
            "No active scored model comparisons here.")
        assert ("2 retired or early-stopped design(s) hidden from active "
                "trials — full record in Full evidence."
                in section["leader"])

        full = _trials_sections({"trials": board}, full=True)
        assert full["count"] == 0
        assert full["table"] == ""
        assert "Test models are paused" in full["leader"]
        assert "6-year" not in full["leader"]
        assert "2 stopped trial comparison(s) on record" in full["how_to_read"]
    finally:
        store.close()
