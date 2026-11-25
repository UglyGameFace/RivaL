"""Board render: short blockers, no mid-sentence cuts, takeaways.

Display-only coverage for the Models board and the Polymarket
contest view: candidate lines always carry a complete blocker
phrase, and the takeaway lines are computed from the recorded
state (change the fixture numbers and the lines change).
"""
from parlay_bot.discord_app import trial_board
from parlay_bot.discord_app.embed_text import fit_embed_field_value
from parlay_bot.discord_app.home import (
    _contest_text,
    contest_takeaway,
    workbench_takeaway,
    workbench_text,
)
from parlay_bot.perpetuals import replay_generation as gen
from parlay_bot.storage.hot import SQLiteHotStore

AT = "2026-11-25T17:00:00+00:00"
LONG_REASON = (
    "its exchange features were not on the settled record it was "
    "registered against; they were recorded under a later schema "
    "and no reader has been wired to that schema for this screen")


def _entry(cid, screen, status, detail, **kw):
    base = {"candidate_id": cid, "screen": screen, "factory": "lab",
            "display": kw.pop("display", cid), "status": status,
            "verdict": None, "graded_rows": 0, "calls": 0, "days": 0,
            "evaluated_at": AT, "detail": detail}
    base.update(kw)
    return base


def _board(tmp_path, entries):
    store = SQLiteHotStore(tmp_path / "board.sqlite")
    gen.record_results(store, entries)
    return gen.read_results_for_board(store)


def test_short_blocker_map_registered(tmp_path):
    board = _board(tmp_path, [
        _entry("gd-1", "G-D", "NOT_MEASURABLE",
               {"cause": "exchange_features_not_recorded"}),
        _entry("gd-2", "G-D", "NOT_MEASURABLE",
               {"cause": "no_post_cutoff_lineage_rows"}),
        _entry("gc-1", "G-C", "NOT_MEASURABLE",
               {"cause": "no_settled_threshold_outcomes"}),
        _entry("liq-cascade-v1", "R2", "DATA-BLOCKED", {}),
        _entry("floor-1", "G-B", "NOT_MEASURABLE",
               {"floors": {"calls": {"have": 30, "need": 250},
                           "call_days": {"have": 3, "need": 7}}}),
        _entry("long-1", "G-X", "NOT_MEASURABLE", {"reason": LONG_REASON}),
        _entry("short-1", "G-X", "NOT_MEASURABLE",
               {"reason": "no rows recorded yet"}),
    ])
    by_id = {c["candidate_id"]: c for c in board["candidates"]}
    assert by_id["gd-1"]["blocker_short"] == (
        "grader built 2026-11-25; grades at the next workbench run")
    assert by_id["gd-2"]["blocker_short"] == (
        "waiting for settled rows with recorded features")
    assert by_id["gc-1"]["blocker_short"] == (
        "outcome recorder added; first settlements Nov 27")
    assert by_id["liq-cascade-v1"]["blocker_short"] == (
        "needs a paid data source; owner's decision")
    assert by_id["floor-1"]["blocker_short"] == "accruing toward its floors"
    # A long recorded reason is never sliced: generic complete phrase.
    assert by_id["long-1"]["blocker_short"] == gen.GENERIC_BLOCKER_SHORT
    assert by_id["long-1"]["blocker"] == LONG_REASON
    # A short recorded reason is already complete and stays verbatim.
    assert by_id["short-1"]["blocker_short"] == "no rows recorded yet"
    # The corrected G-D full text states the correction plainly.
    full = by_id["gd-1"]["blocker"]
    assert "ARE recorded on the live" in full
    assert "no grader read" in full
    assert "built 2026-11-25" in full


GD_ZERO_FLOORS = {"rows": {"have": 0, "need": 500},
                  "days": {"have": 0, "need": 7},
                  "calls": {"have": 0, "need": 100},
                  "clusters": {"have": 0, "need": 40}}


def test_burn_in_candidate_shows_scope_not_zero_data(tmp_path):
    """Live G-D shape: floors all zero, but the recorded scope holds
    tens of thousands of rows inside the walk-forward burn-in."""
    board = _board(tmp_path, [
        _entry("llm-mask-btc-trend", "G-D", "NOT_MEASURABLE",
               {"floors": GD_ZERO_FLOORS,
                "coverage": {"scope_rows": 29507, "scope_days": 5,
                             "scored_days": 0,
                             "features_cutoff": gen.GD_FEATURES_CUTOFF}},
               display="AI-suggested mask · BTC"),
        _entry("llm-mask-eth-trend", "G-D", "NOT_MEASURABLE",
               {"floors": GD_ZERO_FLOORS,
                "coverage": {"scope_rows": 12000, "scope_days": 5,
                             "scored_days": 0}},
               display="AI-suggested mask · ETH"),
        _entry("fit-floor", "G-D", "NOT_MEASURABLE",
               {"floors": GD_ZERO_FLOORS,
                "coverage": {"scope_rows": 40000, "scope_days": 9,
                             "scored_days": 0,
                             "unscored_days_fit_floor": 2,
                             "features_cutoff": gen.GD_FEATURES_CUTOFF}},
               display="Past burn-in, fit floor"),
    ])
    by_id = {c["candidate_id"]: c for c in board["candidates"]}
    btc = by_id["llm-mask-btc-trend"]
    assert btc["blocker_short"] == (
        "in walk-forward burn-in; scoring starts 2026-11-28")
    assert "29,507 rows" in btc["progress"]
    assert "5 days" in btc["progress"]
    assert "0 scored days" in btc["progress"]
    # No recorded cutoff: the phrase carries no invented date.
    assert by_id["llm-mask-eth-trend"]["blocker_short"] == (
        "in walk-forward burn-in; no scored days yet")
    # Past the burn-in window with zero scored days is a different
    # blocker (the fit floor), not burn-in.
    assert by_id["fit-floor"]["blocker_short"] == "accruing toward its floors"
    # The board lines carry the burn-in phrase and the scope counts.
    compact = workbench_text(board)
    assert "in walk-forward burn-in; scoring starts 2026-11-28" in compact
    assert "29,507 rows" in compact
    takeaway = workbench_takeaway(board)
    assert "scoring starts 2026-11-28" in takeaway


def _full_fixture(tmp_path):
    """34 candidates carrying the longest real blocker strings."""
    entries = []
    for i in range(10):
        entries.append(_entry(
            f"gd-{i}", "G-D", "NOT_MEASURABLE",
            {"cause": "exchange_features_not_recorded"},
            display=f"AI-suggested mask {i}"))
    for i in range(8):
        entries.append(_entry(
            f"gc-{i}", "G-C", "NOT_MEASURABLE",
            {"cause": "no_settled_threshold_outcomes"},
            display=f"Options-implied {i}"))
    entries.append(_entry("liq-cascade-v1", "R2", "DATA-BLOCKED", {},
                          display="Liquidation cascade screen"))
    for i in range(10):
        entries.append(_entry(
            f"floor-{i}", "G-B", "NOT_MEASURABLE",
            {"floors": {"calls": {"have": 30 + i, "need": 250},
                        "call_days": {"have": 3, "need": 7}}},
            display=f"Conditioning {i}", graded_rows=900 + i,
            calls=30 + i, days=3))
    for i in range(5):
        entries.append(_entry(
            f"long-{i}", "G-X", "NOT_MEASURABLE",
            {"reason": LONG_REASON}, display=f"Long reason {i}"))
    assert len(entries) == 34
    return _board(tmp_path, entries)


def test_render_never_cuts_a_blocker(tmp_path):
    record = _full_fixture(tmp_path)
    compact = fit_embed_field_value(workbench_text(record))
    assert len(compact) <= 1024
    assert "…" not in compact
    for line in compact.split("\n"):
        for cand in record["candidates"]:
            name = cand["display"]
            if line.startswith(f"• {name}"):
                # The line carries the complete short phrase, whole.
                assert cand["blocker_short"] in line
    full = workbench_text(record, full=True)
    assert len(full) <= 1024
    assert "…" not in full
    for line in full.split("\n"):
        for cand in record["candidates"]:
            if line.startswith(f"• {cand['display']}"):
                # Full evidence lines carry the full text or the
                # candidate is listed whole on a later line -- never
                # a partial slice of its blocker.
                assert cand["blocker"] in line


def test_workbench_takeaway_is_derived(tmp_path):
    record = _full_fixture(tmp_path)
    text = workbench_takeaway(record)
    assert "No model has qualified yet." in text
    # Closest is the floor candidate furthest along its scarcest
    # floor (floor-9: calls 39/250, days 3/7 -> bottleneck days 3/7,
    # tied across the pile; graded rows break the tie).
    assert "Closest: Conditioning 9" in text
    assert "still needs 211 more calls; 4 more days" in text
    assert "10 in G-D (grader built 2026-11-25" in text
    assert "8 in G-C (outcome recorder added; first settlements Nov 27)" in text
    assert "1 in R2 (needs a paid data source; owner's decision)" in text
    assert "the next workbench run grades the accrual-blocked pile" in text
    # Change the numbers and the line changes.
    store = SQLiteHotStore(tmp_path / "changed.sqlite")
    gen.record_results(store, [
        _entry("only", "G-B", "NOT_MEASURABLE",
               {"floors": {"calls": {"have": 249, "need": 250}}},
               display="Nearly there", graded_rows=10, calls=249, days=7)])
    text2 = workbench_takeaway(gen.read_results_for_board(store))
    assert "Closest: Nearly there" in text2
    assert "still needs 1 more calls" in text2
    assert "10 in G-D" not in text2


def test_workbench_takeaway_verdict_follows_passes(tmp_path):
    store = SQLiteHotStore(tmp_path / "passed.sqlite")
    gen.record_results(store, [
        _entry("win", "G-A", "EVALUATED", {}, verdict="PASS",
               graded_rows=100, calls=90, days=9)])
    text = workbench_takeaway(gen.read_results_for_board(store))
    assert "passed the screen" in text
    assert "Nothing is close to qualifying yet." in text


def _contest_models(contracts=352, improvement=0.0012, net=0.004,
                    significant=False):
    return [
        {"model": "odds-lag", "label": "Odds-lag learner",
         "contracts": contracts, "improvement": improvement,
         "net_per_contract": net, "trades": 300,
         "significant": significant},
        {"model": "book", "label": "Book logistic", "contracts": 120,
         "improvement": -0.002, "net_per_contract": -0.01,
         "trades": 100, "significant": False},
    ]


def test_contest_takeaway_is_derived():
    rows = trial_board.contest_rows(_contest_models())
    text = contest_takeaway(rows)
    assert "No contest model has qualified yet." in text
    assert "Odds-lag" in text
    assert "352 graded" in text
    assert "edge +1.2" in text
    assert "+0.4¢ after fees" in text
    assert "ahead but NOT statistically passed" in text
    changed = contest_takeaway(trial_board.contest_rows(
        _contest_models(contracts=900, improvement=0.003, net=-0.002)))
    assert "900 graded" in changed
    assert "edge +3.0" in changed
    assert "-0.2¢ after fees" in changed
    passed = contest_takeaway(trial_board.contest_rows(
        _contest_models(significant=True)))
    assert "have statistically passed" in passed
    assert "statistically passed." in passed


def test_contest_text_within_field_limit():
    models = _contest_models()
    for i in range(12):
        models.append({"model": f"m{i}", "label": f"Model {i}",
                       "contracts": 50 + i, "improvement": -0.001 * i,
                       "net_per_contract": 0.0, "trades": 10,
                       "significant": False})
    text = _contest_text({"contest": models})
    assert text is not None
    # The derived verdict line leads the contest field; the takeaway
    # (closest candidate, caveat) follows directly under it.
    assert text.startswith("**Polymarket contest:**")
    assert "\n**Takeaway:**" in text
    assert len(fit_embed_field_value(text)) <= 1024
    assert len(text) <= 1024
