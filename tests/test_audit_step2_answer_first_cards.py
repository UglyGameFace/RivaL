"""Audit Step 2 (2026-11-10): answer-first cards + one set of names.

1. ONE shared builder (answer_cards) + name table, used by both
   GuidedHome.render (Performance page) and ResearchDashboard._render_full.
2. The name table maps every old term to its new name; internal ids stay
   in the "Raw numbers:" view only.
3. Every converted card answers first (Yes / No / Not yet / Can't tell,
   <=12 words), gives one reason + one next step, and keeps details
   behind the existing selects; the footer carries the single
   disclaimer "Research only. No real trades."
4. Feed status shows on the overview, and elsewhere only if a feed is behind.

Every render here uses realistic worst-case numbers, never placeholder
mockups: a 150-target / 21-day proof candidate, the audit's own
8-of-10-feeds example, and the largest plausible trial counts.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import answer_cards as ac
from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app import research_dashboard as rd_module
from parlay_bot.discord_app.answer_cards import (
    RESEARCH_FOOTER,
    AnswerCard,
    boosted_card,
    costs_card,
    engine_card,
    lineage_card,
    proof_card,
    readiness_card,
    rename_text,
)
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from tests.test_audit_step1_card_quick_fixes import (
    _accuracy_dashboard,
    _analytics_details,
)
from tests.test_performance_overview import (
    _close,
    _fields,
    _make_home,
    _patch_live_reads,
)

# --- 1. The name table -------------------------------------------------------

# Every old term the audit lists, in a sentence where it actually appears.
OLD_TO_NEW = [
    ("simple guess", "coin flip"),
    ("baseline", "coin flip"),
    ("basic forecast", "coin flip"),
    ("guess 0.250", "coin flip 0.250"),
    ("incumbent", "main model"),
    ("live model", "main model"),
    ("mathematical model", "main model"),
    ("challenger", "test model"),
    ("shadow", "test model"),
    ("family", "test model"),
    ("trial", "test model"),
    ("pooled", "6-year history model"),
    ("boosted", "6-year history model"),
    ("advanced", "6-year history model"),
    ("regime_v1", "trend-only test"),
    ("regime_live_v1", "trend learner"),
    ("regime_live_fast_v1", "fast trend learner"),
    ("gaussian_nb_v1", "pattern test A"),
    ("knn21_v1", "pattern test B"),
    ("Brier", "error score"),
    ("paired Brier gain", "paired error score gain"),
    ("quiet", "calm"),
    ("turbulent", "wild"),
    ("regime", "market"),
    ("settled", "graded"),
    ("scored", "graded"),
    ("decided", "graded"),
    ("paper", "practice trade"),
    ("shadow quote", "practice trade"),
    ("simulated", "practice trade"),
]


@pytest.mark.parametrize("old,new", OLD_TO_NEW)
def test_name_table_maps_every_old_term(old, new):
    assert ac.DISPLAY_NAMES[old.lower()] == new
    renamed = rename_text(f"The {old} beats the {old}.")
    assert new.lower() in renamed.lower()
    assert old.lower() not in renamed.lower()


def test_rename_is_idempotent_and_preserves_raw_numbers():
    text = "Brier 0.250 vs incumbent; regime_v1 settled 5 paper results"
    once = rename_text(text)
    assert rename_text(once) == once
    assert "regime_v1" not in once
    raw = rename_text("Head line here\nRaw numbers:\nregime_v1 Brier settled 5")
    head, _, tail = raw.partition("Raw numbers:")
    assert "regime_v1" not in head
    assert "regime_v1 Brier settled 5" in tail  # ids stay matchable in raw


def test_proof_bars_match_family_governance():
    from parlay_bot.perpetuals.family_governance import MIN_DAYS, MIN_WINDOWS

    assert ac.PROOF_MIN_DAYS == MIN_DAYS == 21
    assert ac.PROOF_MIN_TARGETS == MIN_WINDOWS == 150


# --- 2. The answer-first builder ----------------------------------------------

def test_answer_card_requires_a_verdict_first_line_within_12_words():
    card = AnswerCard(
        question="Q?", verdict="Not yet",
        answer="Not yet — no test model passed proof.",
        reason="Still missing outcomes.",
        next_step="Day 3 of 21 — 12 targets so far.",
    )
    assert card.field_value().split("\n")[0] == "Not yet — no test model passed proof."
    assert len(card.answer.split()) <= 12
    with pytest.raises(ValueError):
        AnswerCard(question="Q?", verdict="Maybe",
                   answer="Maybe so.", reason="r", next_step="n")
    with pytest.raises(ValueError):  # answer must start with the verdict
        AnswerCard(question="Q?", verdict="Yes",
                   answer="No — not this.", reason="r", next_step="n")
    with pytest.raises(ValueError):  # 13 words
        AnswerCard(question="Q?", verdict="Yes",
                   answer="Yes — one two three four five six seven eight nine ten eleven.",
                   reason="r", next_step="n")
    with pytest.raises(ValueError):  # 4 numbers in one paragraph (rule 4)
        AnswerCard(question="Q?", verdict="Yes", answer="Yes — done.",
                   reason="1, 2, 3 and 4 things happened.", next_step="n")


# --- 3. The two named cards ----------------------------------------------------

def _candidate(*, name="regime_live_v1", targets=150, days=21, qualified=False,
               checks=None, coverage=0.93, closed=140, eligible=150):
    return {
        "name": name, "valid": True, "targets": targets, "days": days,
        "eligible_quotes": eligible, "closed_quotes": closed,
        "quote_coverage": coverage, "trades": 60, "gain": 0.004,
        "checks": checks or {
            "future_outcomes": True, "beats_baselines": True,
            "quote_coverage": True, "after_cost_paper": True,
            "valid_records": True,
        },
        "qualified_paper": qualified,
    }


def _proof(*, evaluations):
    return {"status": "recorded", "evaluations": evaluations, "reason": "x"}


def test_proof_card_not_yet_shows_progress_toward_the_target():
    """Worst case that still fails: 149 targets on day 20, one check short."""
    candidate = _candidate(
        targets=149, days=20,
        checks={"future_outcomes": False, "beats_baselines": True,
                "quote_coverage": True, "after_cost_paper": False,
                "valid_records": True})
    card = proof_card(_proof(evaluations=[{
        "horizon": 5, "venue": "hyperliquid", "contract": "BTC",
        "fresh": True, "candidate": candidate,
        "reason": "Still missing: outcomes, cost", "paper_qualified": False,
    }]), raw_text="5m regime_live_v1: outcomes ✗")
    value = card.field_value()
    assert value.split("\n")[0] == "Not yet — no test model passed proof."
    assert "trend learner is still missing: outcomes, cost." in value
    assert "Day 20 of 21 — 149 targets so far." in value
    assert "Needs 150 targets to qualify." in value
    assert "regime_live_v1" in value.split("Raw numbers:")[1]  # raw keeps ids
    assert "regime_live_v1" not in value.split("Raw numbers:")[0]


def test_proof_card_yes_and_cant_tell():
    candidate = _candidate(qualified=True)
    card = proof_card(_proof(evaluations=[{
        "horizon": 5, "venue": "hyperliquid", "contract": "BTC",
        "fresh": True, "candidate": candidate,
        "reason": "Source-exact practice-only research proof passed",
        "paper_qualified": True,
    }]), raw_text="5m regime_live_v1: outcomes ✓")
    assert card.field_value().split("\n")[0] == "Yes — a test model passed paper proof."
    assert "trend learner passed all five proof checks on 5m." in card.field_value()
    card = proof_card(_proof(evaluations=[]), raw_text="No evaluations.")
    assert card.field_value().split("\n")[0] == "Can't tell — no proof evaluation recorded."
    candidate = _candidate()
    candidate["valid"] = False
    card = proof_card(_proof(evaluations=[{
        "horizon": 5, "venue": "hyperliquid", "contract": "BTC",
        "fresh": True, "candidate": candidate,
        "reason": "Saved evaluation is invalid", "paper_qualified": False,
    }]), raw_text="invalid")
    assert card.field_value().split("\n")[0] == "Can't tell — saved proof cannot be verified."


def test_lineage_card_partly_no_and_cant_tell():
    partly = lineage_card(
        [{"lineage_proven": 10, "historically_seeded": 3,
          "forecasts": 10, "lineage_unknown": 0,
          "latest_historical_seed_sha256": "abc",
          "latest_online_training_updates": 5,
          "online_training_updates_low": 5,
          "online_training_updates_high": 5,
          "online_training_venues": 1}],
        details=("5-minute: 3 of 10 predictions used history.",),
        raw="**5-min:** history used in 3 of the last 10 predictions.")
    assert partly.field_value().split("\n")[0] == (
        "Yes — partly, 3 of 10 predictions used history.")
    none = lineage_card(
        [{"lineage_proven": 10, "historically_seeded": 0,
          "forecasts": 10, "lineage_unknown": 0,
          "latest_historical_seed_sha256": None,
          "latest_online_training_updates": 5,
          "online_training_updates_low": 5,
          "online_training_updates_high": 5,
          "online_training_venues": 1}])
    assert none.field_value().split("\n")[0] == (
        "No — the main model learns from live candles only.")
    empty = lineage_card([{"lineage_proven": 0, "historically_seeded": 0}])
    assert empty.field_value().split("\n")[0] == (
        "Can't tell — no training history checked yet.")


def test_boosted_readiness_engine_and_costs_cards_answer_first():
    boosted = boosted_card({
        "portable_imported": 54, "portable_screened": 0,
        "screening_blockers": [
            {"reason": "fewer_than_six_source_months", "count": 12},
            {"reason": "validation_brier_gain_below_0.005", "count": 8},
        ],
        "issued": 120, "settled": 90, "quote_exits": 3,
        "fresh_paper_promotions": [],
    }, raw="Advanced models built from history so far: 54")
    value = boosted.field_value()
    assert value.split("\n")[0] == "Not yet — no 6-year history model is ready."
    assert "0 of 54 passed screening." in value
    assert "54" in value.split("Raw numbers:")[1]  # raw keeps the original text
    ready = readiness_card({
        "eligible": 3, "configured": 9, "best_live_streak": 130,
        "min_live_bars": 150, "best_causal_5m": 120, "best_evaluated_5m": 45,
        "min_evaluations": 50, "stale_or_blocked": 2, "waiting_history": 4,
    })
    assert ready.field_value().split("\n")[0] == (
        "Not yet — 3 of 9 exchanges can predict.")
    assert "Needs 150 live bars; longest streak is 130." in ready.field_value()
    running = engine_card(state="running", age_minutes=3, issued=20,
                          settled=18, raw="math OK · settlement OK")
    assert running.field_value().split("\n")[0] == "Yes — the learning loop is running."
    stalled = engine_card(state="stale", age_minutes=45, issued=0,
                          settled=0, raw="stale")
    assert stalled.field_value().split("\n")[0] == "No — the learning loop has stalled."
    costs = costs_card(raw="**NOT VERIFIED.** No fills.")
    assert costs.field_value().split("\n")[0] == (
        "Not yet — profit after fees is not verified.")
    assert "16, 40 and 80 bps" in costs.field_value()


# --- 4. Rendered cards ----------------------------------------------------------

def _proof_dict(*, targets=149, days=20, qualified=False):
    candidate = _candidate(targets=targets, days=days, qualified=qualified,
                           checks={"future_outcomes": targets >= 150,
                                   "beats_baselines": True,
                                   "quote_coverage": True,
                                   "after_cost_paper": qualified,
                                   "valid_records": True})
    return _proof(evaluations=[{
        "horizon": 5, "venue": "hyperliquid", "contract": "BTC",
        "fresh": True, "candidate": candidate,
        "reason": "Still missing: outcomes", "paper_qualified": qualified,
    }])


@pytest.mark.asyncio
async def test_progress_section_renders_the_proof_card(monkeypatch):
    """The full proof table lives under Raw numbers; the head answers first
    and carries no internal family id."""

    monkeypatch.setattr(rd_module, "proof_progress",
                        lambda *_, **__: _proof_dict())
    from tests.test_audit_step1_card_quick_fixes import _FakeRegimeMonitor
    monkeypatch.setattr(rd_module, "ForwardAccuracyMonitor", _FakeRegimeMonitor)
    dashboard = ResearchDashboard(
        owner_id=42, market=SimpleNamespace(store=object()), ai=None,
        venues=("hyperliquid",),
    )
    dashboard.shadow_enabled = True
    dashboard.screen = "accuracy"
    dashboard.section = "progress"
    dashboard._rebuild()
    now = datetime.now(UTC)
    dashboard._live_feed_status = lambda: (8, 10, now, now)
    try:
        embed = await dashboard._render_full()
    finally:
        dashboard.stop()
    fields = {f.name: f.value for f in embed.fields}
    assert embed.footer.text == RESEARCH_FOOTER
    proof = fields["Future model proof"]
    head, _, raw = proof.partition("Raw numbers:")
    assert head.split("\n")[0] == "Not yet — no test model passed proof."
    assert "Day 20 of 21 — 149 targets so far." in head
    assert "regime_live_v1" not in head
    assert "regime_live_v1" in raw
    assert "Brier" not in head and "incumbent" not in head
    # The regime splits and trial detail ride along, renamed.
    assert "Prospective skill by market" in fields
    assert "calm" in fields["Prospective skill by market"]
    assert "Test-model detail (5-minute)" in fields


@pytest.mark.asyncio
async def test_lineage_section_renders_the_lineage_card(monkeypatch):
    class _FakeMonitor:
        def __init__(self, market) -> None:
            pass

        def learning_lineage_summary(self, *, asset, horizon_minutes):
            return {
                "forecasts": 10, "lineage_proven": 10,
                "historically_seeded": 3, "lineage_unknown": 0,
                "latest_lineage_verified": True,
                "latest_historical_seed_sha256": "abcdef0123456789",
                "latest_historical_training_labels": 100,
                "latest_online_training_updates": 5,
                "online_training_updates_low": 5,
                "online_training_updates_high": 5,
                "online_training_venues": 1,
            }

        def boosted_production_evidence(self, *, asset):
            return {
                "portable_imported": 54, "portable_screened": 0,
                "screening_blockers": [
                    {"reason": "fewer_than_six_source_months", "count": 12}],
                "issued": 120, "settled": 90, "quote_exits": 3,
                "fresh_paper_promotions": [], "status_reason": "not qualified",
                "scopes": [],
            }

        def summary(self, *, asset):
            return {"last_7d": {5: {"issued": 10, "settled": 10,
                                    "first_issued": None},
                                15: {"issued": 0, "settled": 0,
                                     "first_issued": None},
                                60: {"issued": 0, "settled": 0,
                                     "first_issued": None}},
                    "asset": asset}

    monkeypatch.setattr(rd_module, "ForwardAccuracyMonitor", _FakeMonitor)
    dashboard = ResearchDashboard(
        owner_id=42, market=SimpleNamespace(store=object()), ai=None,
        venues=("hyperliquid",),
    )
    dashboard.shadow_enabled = True
    dashboard.screen = "accuracy"
    dashboard.section = "lineage"
    dashboard._rebuild()
    now = datetime.now(UTC)
    dashboard._live_feed_status = lambda: (8, 10, now, now)
    try:
        embed = await dashboard._render_full()
    finally:
        dashboard.stop()
    fields = {f.name: f.value for f in embed.fields}
    assert embed.footer.text == RESEARCH_FOOTER
    learning = fields["How is RivaL learning?"]
    assert learning.split("\n")[0] == (
        "Yes — partly, 9 of 30 predictions used history.")
    assert "6-year history model" in learning
    assert "Are 6-year history models ready?" in fields
    boosted = fields["Are 6-year history models ready?"]
    assert boosted.split("\n")[0] == "Not yet — no 6-year history model is ready."
    assert "0 of 54 passed screening." in boosted


@pytest.mark.asyncio
async def test_feed_status_only_when_it_is_the_news(monkeypatch):
    """Rule 6: the audit's own example — 8 of 10 feeds — is news
    everywhere; 10 of 10 is news only on the overview."""
    dashboard = _accuracy_dashboard(monkeypatch, section="five")
    now = datetime.now(UTC)
    try:
        dashboard._live_feed_status = lambda: (8, 10, now, now)
        embed = await dashboard._render_full()
        assert "Live market feeds" in [f.name for f in embed.fields]
        dashboard._live_feed_status = lambda: (10, 10, now, now)
        embed = await dashboard._render_full()
        assert "Live market feeds" not in [f.name for f in embed.fields]
        # Healthy feeds are news on Running details (the old overview now
        # opens Models on trial).
        dashboard.section = "running"
        embed = await dashboard._render_full()
        assert "Live market feeds" in [f.name for f in embed.fields]
    finally:
        dashboard.stop()


@pytest.mark.asyncio
async def test_forward_eval_and_calibration_cards_answer_first(monkeypatch):
    """5 graded at 100% is Not yet (Step 1 still holds); the calibration
    card never claims a pass — its best status is a preliminary screen."""
    monkeypatch.setattr(
        rd_module, "analytics_snapshot",
        lambda *_, **__: _analytics_details())
    dashboard = _accuracy_dashboard(monkeypatch, section="calibration")
    try:
        embed = await dashboard._render_full()
    finally:
        dashboard.stop()
    fields = {f.name: f.value for f in embed.fields}
    five = fields["Forward 5-minute evaluation"]
    assert five.split("\n")[0] == "Not yet — too few graded results to judge."
    assert "Only 5 graded results so far." in five
    assert "too few to judge — 5 scored" in five  # raw keeps the original text
    calibration = fields["Forward 5m calibration"]
    assert calibration.split("\n")[0].startswith(("Not yet", "Can't tell", "No"))
    assert "Yes —" not in calibration.split("\n")[0]
    assert embed.footer.text == RESEARCH_FOOTER


# --- 5. The Performance page -----------------------------------------------------

@pytest.mark.asyncio
async def test_performance_page_uses_the_one_name_set(tmp_path, monkeypatch):
    """Worst case from Step 1 (77,760 rows, 8,640 windows) with every
    trial model listed: no old term survives in any field, and the footer
    carries the single disclaimer."""
    from parlay_bot.discord_app import trial_board

    bot, home = _make_home(tmp_path)
    rows = trial_board.fold([("okx", "BTC-okx", {
        family: {"days": 9, "targets": 400, "forward_gain": .001}
        for family in ("regime_v1", "regime_live_v1", "regime_live_fast_v1",
                       "regime_wild_v1", "pooled_5m_1a10acfb6945", "cross_v1", "liq_v1")})],
        [])
    _patch_live_reads(monkeypatch, trials={"rows": rows, "markets": 1, "trials": 7,
                                           "passed": 0, "stops": 0})
    monkeypatch.setattr(
        home_module, "_cycle_coverage",
        lambda *_: {"cycles": 288, "expired": 199, "issued": 100})
    try:
        embed = await home.render()
        fields = _fields(embed)
    finally:
        await _close(bot, home)
    page = "\n".join([embed.description or "", *fields.values()])
    lowered = page.lower()
    for old in ("simple guess", "brier", "incumbent", "regime_v1",
                "regime_live_v1", "live-learning", "pooled 6-year model",
                "price feeds", "shadow", "challenger", "settled"):
        assert old not in lowered, old
    for new in ("coin flip", "error", "main model", "trend-only", "trend learn",
                "6-year", "exchanges live", "graded"):
        assert new in lowered, new
    assert embed.footer.text == RESEARCH_FOOTER + " · Per-model detail: Detailed Research"
    assert "Research only. No real trades." in embed.footer.text
