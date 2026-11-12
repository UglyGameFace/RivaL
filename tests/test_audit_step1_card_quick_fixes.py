"""Audit Step 1 (2026-11-10): card quick fixes.

1. No field may be cut mid-word at Discord's 1024-char limit: fields are
   fitted by dropping tail lines, never sliced.
2. The regime_v1 per-regime gain line is hidden: it is +0.0000 by design
   (regime_v1 returns the incumbent's own probability verbatim), so it can
   never show anything else. The Performance-page trial row and the Trial
   detail field still report regime_v1's honest trial result.
3. No rate on fewer than 20 results: "too few to judge (N)".
4. The 24-hour line shows a warning, not a green tick, when predictions
   were skipped.

Every render here uses realistic worst-case numbers, never placeholder
mockups: the longest real regime status
("insufficient_distinct_market_conditions"), four-digit gains, the
largest plausible trial counts, and the audit's own example
(288 of 288 cycles, 199 predictions skipped).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app import research_dashboard as rd_module
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from tests.test_performance_overview import (
    _all_fields,
    _close,
    _make_home,
    _patch_live_reads,
)


def _worst_proof() -> dict:
    """Longest real regime status, worst-case gains on every regime."""
    return {
        "status": "insufficient_distinct_market_conditions",
        "regimes": {
            "quiet": {"paired_brier_gain": -0.1234},
            "normal": {"paired_brier_gain": 0.1234},
            "turbulent": {"paired_brier_gain": -0.0123},
        },
    }


class _FakeRegimeMonitor:
    """ForwardAccuracyMonitor stub returning the worst-case proof."""

    def __init__(self, market) -> None:
        self.proof = _worst_proof()

    def regime_summary(self, *, asset, horizon_minutes, family=None):
        return self.proof


def _dashboard(monkeypatch) -> ResearchDashboard:
    monkeypatch.setattr(
        rd_module, "ForwardAccuracyMonitor", _FakeRegimeMonitor)
    dashboard = ResearchDashboard(
        owner_id=42, market=SimpleNamespace(), ai=None,
        venues=("hyperliquid",),
    )
    return dashboard


# --- 1. The field fitter -------------------------------------------------

def test_fit_field_short_values_pass_through_unchanged():
    text = "short value\nsecond line"
    assert home_module.fit_embed_field_value(text) == text


def test_fit_field_drops_tail_lines_instead_of_slicing_mid_word():
    lines = [
        "first line stays",
        "second line stays",
        "third line is the lowest priority and goes",
    ]
    text = "\n".join(lines)
    # Pad the tail line so the whole value exceeds the budget.
    text += "\n" + "x " * 600
    fitted = home_module.fit_embed_field_value(text, budget=64)
    assert fitted == "first line stays\nsecond line stays"
    assert len(fitted) <= 64


def test_fit_field_single_long_line_cuts_at_a_word_boundary():
    text = " ".join(["word"] * 400)  # one 1999-char line, no newlines
    fitted = home_module.fit_embed_field_value(text)
    assert len(fitted) <= 1024
    assert fitted.endswith("…")
    assert fitted[:-1].split()[-1] == "word"


def test_fit_field_never_exceeds_the_budget():
    cases = [
        "\n".join(f"line {i} " + "y " * 50 for i in range(10)),
        " ".join(["longword"] * 300),
        "a" * 5000,
        "short",
    ]
    for text in cases:
        assert len(home_module.fit_embed_field_value(text)) <= 1024


def test_rate_or_too_few_hides_rates_below_20():
    assert home_module.rate_or_too_few(19, "100.0%", what="decided") == (
        "too few to judge — 19 decided")
    assert home_module.rate_or_too_few(0, "0.0%", what="sat out") == (
        "too few to judge — 0 sat out")
    assert home_module.rate_or_too_few(20, "50.0%", what="decided") == "50.0%"
    assert home_module.rate_or_too_few(77760, "100.0%", what="decided") == "100.0%"


# --- 1+2. The regime field -------------------------------------------------

@pytest.mark.asyncio
async def test_regime_field_hides_always_zero_line_and_fits(monkeypatch):
    """Worst-case proof: the old code sliced this field at exactly 1024
    chars, cutting the disclaimer mid-word ("...is not tradin"). The new
    field keeps every remaining line whole and hides regime_v1, whose
    per-regime gain is +0.0000 by design."""
    dashboard = _dashboard(monkeypatch)
    try:
        value = await dashboard._regime_field_value()
    finally:
        dashboard.stop()
    assert "regime_v1" not in value
    assert "trend learner 5m" in value
    assert "fast trend learner 15m" in value
    assert "main model 5m" in value
    assert "A stable shadow score is not trading profit." in value
    assert len(value) < 1024
    assert not value.endswith("tradin")


# --- 3+4. The Performance page ---------------------------------------------

def _worst_exchange_part(now) -> dict:
    """Maximally inconsistent seven-day score: every consistency check
    fires, the overdue line shows, and both trial rows are extreme."""
    return {"last_7d": {5: {
        "decided": 77760, "correct": 77761, "hit_rate": 0.5,
        "issued": 77760, "settled": 77761, "scored": 77762,
        "brier": .001, "baseline_brier": .999,
        "due": 99999, "overdue": 50000,
        "venues": 9, "windows": 8640,
        "first_issued": (now - timedelta(days=30)).isoformat(),
    }}}


def _worst_polymarket_status():
    return {
        "settled_forward_contracts": 300, "learned_contracts": 402,
        "score_window_days": 7, "forward_brier": .2387,
        "market_baseline_brier": .2222, "observed_contracts": 900,
        "pooled": {"contracts": 815, "model_brier": .252, "market_brier": .229,
                   "value_bets": 815, "value_bet_wins": 268,
                   "value_bet_avg_return": -0.125},
        "kalshi": {"contracts": 500, "model_brier": .240, "market_brier": .230,
                   "value_bets": 100, "value_bet_wins": 40,
                   "return_per_dollar_after_fees": -0.05},
        "kalshi_challengers": {
            "flow_v1": {"contracts": 300, "model_brier": .250,
                        "market_brier": .240, "value_bets": 50,
                        "value_bet_wins": 20, "return_per_dollar_after_fees": -0.03},
            "flow_v2": {"contracts": 250, "model_brier": .245,
                        "market_brier": .240, "value_bets": 60,
                        "value_bet_wins": 25, "return_per_dollar_after_fees": -0.02},
        },
    }


@pytest.mark.asyncio
async def test_performance_page_worst_case_fields_fit_and_tell_the_truth(
        tmp_path, monkeypatch):
    """The audit's own example: 288 of 288 cycles ran, 199 predictions
    skipped — and Intelligence agreed only twice. The 24-hour line must be
    ⚠️, a 2-call rate is "too few to judge", and no field may reach 1024
    even with every number at its widest."""
    from parlay_bot.discord_app.trial_board import TrialRow

    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    rows = [TrialRow(name=f"Model {i:02d}", days=99, results=999999, gain=-.999,
                     ahead=9, exchanges=9, stopped=0, passed=False, after_fees_bps=-99.9)
            for i in range(40)]
    _patch_live_reads(monkeypatch, trials={"rows": rows, "markets": 12, "trials": 999,
                                           "passed": 0, "stops": 999})
    monkeypatch.setattr(
        home_module, "_performance_exchange",
        lambda *_: _worst_exchange_part(now))
    monkeypatch.setattr(
        home_module, "_cycle_coverage",
        lambda *_: {"cycles": 288, "expired": 199, "issued": 100})
    monkeypatch.setattr(
        home_module, "_intelligence_status",
        lambda *_: {
            "latest": {"stances": {5: ("follow", "up")}},
            "report": {"followed": 2, "followed_hit_rate": 0.0,
                       "abstained": 2, "model_hit_when_abstained": 0.0},
        })
    home.polymarket_status = lambda *_: _worst_polymarket_status()
    try:
        embeds = await home.render_embeds()
        fields = _all_fields(embeds)
    finally:
        await _close(bot, home)
    assert max(map(len, fields.values())) < 1024, {
        name: len(value) for name, value in fields.items()}
    assert all(len(e) < 6000 for e in embeds)
    assert sum(len(e) for e in embeds) <= 5900
    trials = next(v for k, v in fields.items() if k.startswith("🧪"))
    assert "… and 26 more" in trials        # 14 rows shown, the rest counted
    health = fields["⚙️ Running and learning"]
    assert "too few to judge — 2 followed" in health
    assert "right 0%" not in health
    assert "⚠️ 288/288 cycles in 24h, 199 predictions skipped (ran late)" in health
    assert "✅ 288/288" not in health


@pytest.mark.asyncio
async def test_24h_tick_stays_green_when_nothing_was_skipped(
        tmp_path, monkeypatch):
    """Control: a clean 288/288 with zero skipped predictions is still ✅."""
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(
        home_module, "_cycle_coverage",
        lambda *_: {"cycles": 288, "expired": 0, "issued": 100})
    try:
        fields = _all_fields(await home.render_embeds())
    finally:
        await _close(bot, home)
    assert "✅ 288/288 cycles in 24h" in fields["⚙️ Running and learning"]
    assert "predictions skipped" not in fields["⚙️ Running and learning"]


@pytest.mark.asyncio
async def test_exchange_accuracy_hides_rate_below_20(tmp_path, monkeypatch):
    """_score()'s default is 3 of 7: a 42.9% headline on 7 results is
    noise, not evidence. At 15 of 30 the rate shows."""
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    try:
        fields = _all_fields(await home.render_embeds())
    finally:
        await _close(bot, home)
    score = fields["🎯 Scoreboard (last 7 days)"]
    assert "too few to judge — 7 graded" in score
    assert "42.9%" not in score

    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: {
        "last_7d": {5: {"decided": 30, "correct": 15, "hit_rate": 0.5,
                        "issued": 40, "settled": 30, "scored": 30,
                        "brier": .24, "baseline_brier": .25}}})
    try:
        fields = _all_fields(await home.render_embeds())
    finally:
        await _close(bot, home)
    score = fields["🎯 Scoreboard (last 7 days)"]
    assert "right **50.0%**" in score and "(15 of 30)" in score


def _tracking_result(*, settled=5, correct=5, hit_rate=1.0) -> dict:
    return {"cohorts": [{
        "horizon": 5, "contract": "hyperliquid/BTC", "issued": settled,
        "target": 25, "settled": settled, "correct": correct,
        "hit_rate": hit_rate, "model_brier": 0.2000,
        "baseline_brier": 0.2500, "weight_fingerprints": 1,
        "verified_historical_seed_count": 0, "invalid": 0,
        "unverified_lineage": 0, "complete": False,
        "early_gain": None, "later_gain": None, "trend": None,
    }]}


def test_tracker_text_hides_rate_below_20():
    from parlay_bot.perpetuals.forward_tracking import tracking_text
    small = tracking_text(_tracking_result(settled=5, correct=5))
    assert "too few to judge — 5 scored" in small
    assert "100.0%" not in small
    scored = tracking_text(_tracking_result(settled=25, correct=15, hit_rate=0.6))
    assert "(60.0%)" in scored


def _polymarket_tracking_result(*, picked=3, wins=2, priced=2,
                                return_per_dollar=-0.05) -> dict:
    return {"cohorts": [{
        "horizon": 5, "issued": 5, "target": 25, "settled": 4,
        "picked": picked, "wins": wins, "waits": 1, "priced": priced,
        "return_per_dollar": return_per_dollar,
        "model_brier": 0.2000, "market_brier": 0.2500, "recent": [],
    }]}


def test_polymarket_tracker_text_hides_rates_below_20():
    from parlay_bot.perpetuals.polymarket_tracking import (
        polymarket_tracking_text,
    )
    small = polymarket_tracking_text(_polymarket_tracking_result())
    assert "too few to judge — 3 picked" in small
    assert "too few to judge — 2 picks with a recorded price" in small
    assert "67%" not in small
    scored = polymarket_tracking_text(_polymarket_tracking_result(
        picked=25, wins=15, priced=25, return_per_dollar=0.10))
    assert "(60%)" in scored
    assert "**+10.0%** per $1" in scored


# --- 3. Detailed Research accuracy rates ------------------------------------

def _summary_report(*, settled5=5, scored5=5, hit5=1.0,
                    settled15=30, scored15=30, hit15=0.6) -> dict:
    def window(settled, scored, hit):
        return {"issued": settled + 2, "settled": settled, "scored": scored,
                "venues": 3, "hit_rate": hit, "brier": 0.2000,
                "baseline_brier": 0.2500, "model_beats_baseline": None,
                "first_issued": None}
    return {
        "last_7d": {5: window(settled5, scored5, hit5),
                    15: window(settled15, scored15, hit15)},
        "prior_7d": {5: window(0, 0, None), 15: window(0, 0, None)},
    }


class _FakeSummaryMonitor:
    def __init__(self, market) -> None:
        pass

    def summary(self, *, asset):
        return _summary_report()


def _accuracy_dashboard(monkeypatch, *, section) -> ResearchDashboard:
    monkeypatch.setattr(rd_module, "ForwardAccuracyMonitor", _FakeSummaryMonitor)
    dashboard = ResearchDashboard(
        owner_id=42, market=SimpleNamespace(store=object()), ai=None,
        venues=("hyperliquid",),
    )
    dashboard.shadow_enabled = True
    dashboard.screen = "accuracy"
    dashboard.section = section
    dashboard._rebuild()
    now = datetime.now(UTC)
    dashboard._live_feed_status = lambda: (8, 10, now, now)
    return dashboard


@pytest.mark.asyncio
async def test_forward_eval_hides_hit_rate_below_20(monkeypatch):
    """5 settled at 100% is not a hit rate yet; 30 settled at 60% is."""
    dashboard = _accuracy_dashboard(monkeypatch, section="five")
    try:
        embed = await dashboard._render_full()
    finally:
        dashboard.stop()
    fields = {f.name: f.value for f in embed.fields}
    assert "too few to judge — 5 scored" in fields["Forward 5-minute evaluation"]
    assert "100.0%" not in fields["Forward 5-minute evaluation"]
    assert "60.0%" in fields["Forward 15-minute evaluation"]
    assert max(map(len, fields.values())) < 1024


def _analytics_details() -> dict:
    def math_horizon():
        return {
            "last_7d": {"brier": 0.2000, "ece": 0.0100, "events": 30},
            "trend": "flat",
            "cost_stress_7d": {
                "scenarios": {"severe_stress": {
                    "mean_net_bps": -5.0, "assumed_total_bps": 80,
                    "status": "not_qualified"}},
                "unique_decided_targets": 30, "utc_days": 7},
            "forward_reliability_7d": {
                "ece": 0.0100, "unique_targets": 30, "days": 7,
                "disputed_targets": 0, "status": "insufficient", "bins": []},
        }
    return {
        "math": {5: math_horizon(), 15: math_horizon()},
        "model_governance": {5: None, 15: None},
        "issued_after_window_opened_7d": {5: 0, 15: 0},
        "math_pending": 0,
        "historical_external_ai": {
            "issued": 10, "settled": 5, "decided": 5, "abstained": 2,
            "directional_accuracy": 1.0},
    }


@pytest.mark.asyncio
async def test_historical_external_ai_hides_hit_rate_below_20(monkeypatch):
    """5 decided at 100% is not a hit rate yet."""
    monkeypatch.setattr(
        rd_module, "analytics_snapshot",
        lambda *_, **__: _analytics_details())
    dashboard = _accuracy_dashboard(monkeypatch, section="calibration")
    try:
        embed = await dashboard._render_full()
    finally:
        dashboard.stop()
    fields = {f.name: f.value for f in embed.fields}
    assert "too few to judge — 5 graded" in fields[
        "Historical external AI · forward outcomes"]
    assert "100.0%" not in fields["Historical external AI · forward outcomes"]
    assert max(map(len, fields.values())) < 1024
