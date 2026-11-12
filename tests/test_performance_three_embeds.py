"""Bot Performance as three embeds: every number from the old card, reorganized.

Fixture numbers mirror the owner's Nov 11 screenshot (BTC 5-min) so the
test proves the new layout carries the same data — they are test data, not
production values: the page still computes everything from its readers.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app.trial_board import TrialRow
from tests.test_performance_overview import (
    _all_fields,
    _close,
    _make_home,
    _patch_live_reads,
)

NOW = datetime.now(UTC)


def _row(name, days, results, gain, *, calls=0, right=0, ahead=0, exchanges=1,
         after_fees=None, family=""):
    return TrialRow(name=name, days=days, results=results, gain=gain, ahead=ahead,
                    exchanges=exchanges, stopped=0, passed=False,
                    after_fees_bps=after_fees, family=family, calls=calls, right=right)


def _reports():
    bands = [
        {"low": .50, "high": .55, "n": 8977, "said": .52, "came_true": 4668,
         "happened": .52},
        {"low": .55, "high": .60, "n": 5427, "said": .57, "came_true": 2931,
         "happened": .54},
        {"low": .60, "high": .65, "n": 1750, "said": .62, "came_true": 945,
         "happened": .54},
        {"low": .65, "high": 1.0, "n": 492, "said": .67, "came_true": 256,
         "happened": .52},
    ]
    regimes = {
        "quiet": {"n": 10910, "came_true": 5684, "hit_rate": .521, "brier": .2504,
                  "baseline_brier": .25},
        "normal": {"n": 5530, "came_true": 2997, "hit_rate": .542, "brier": .2519,
                   "baseline_brier": .25},
        "turbulent": {"n": 206, "came_true": 138, "hit_rate": .67, "brier": .2308,
                      "baseline_brier": .25},
    }
    trials = [
        _row("6-year", 8, 2100, .0026, calls=2000, right=1080, ahead=1, exchanges=6,
             after_fees=.5, family="pooled_5m_1a10acfb6945"),
        _row("Calibrated", 8, 1900, .0019, calls=1900, right=1007),
        _row("Fast trend", 3, 140, .006, calls=140, right=84),
        _row("Trend learn", 3, 140, .0071, calls=140, right=83),
        _row("Trend-only", 4, 200, 0.0, calls=200, right=122),
        _row("Cross-all", 0, 0, None, calls=100, right=47),
        _row("Naive Bayes", 12, 2900, -.0053, calls=2900, right=1508),
        _row("Nearest-21", 12, 2900, -.0066, calls=2900, right=1479),
    ]
    best = {
        5: _row("Trend learn", 3, 140, .0071),
        15: _row("6-year", 8, 696, .008),
        60: _row("6-year", 8, 171, .0234),
    }
    return {
        "polymarket": {
            "settled_forward_contracts": 2001,
            "learned_contracts": 3408,
            "forward_brier": .233, "market_baseline_brier": .231,
            "pooled": {"contracts": 1938, "model_brier": .250, "market_brier": .230,
                       "real_value_bets": 1023, "real_value_bet_wins": 344,
                       "real_return_per_dollar_after_fees": -.121},
        },
        "exchange": {"last_7d": {5: {
            "decided": 16646, "correct": 8820, "hit_rate": 8820 / 16646,
            "issued": 16921, "settled": 16912, "brier": .251,
            "baseline_brier": .250, "venues": 9,
        }}},
        "calibration": {"bands": bands, "regimes": regimes},
        "trials": {"rows": trials, "markets": 8, "trials": 115, "passed": 0,
                   "stops": 102, "venues": ["hyperliquid"], "partial": False},
        "best": {"best": best, "lab": {"15": {
            "tried": True, "active": [1] * 12, "closed": 0,
            "best": {"family": "lab_15m_2e87c2", "fitness": 1.0}}}},
        "contest": [
            {"model": "book-logistic-v1", "label": "Book logistic", "contracts": 1500,
             "trades": 800, "improvement": -.0026, "net_per_contract": -.009,
             "significant": False},
            {"model": "pooled-5ee9-v1", "label": "Pooled 5ee9", "contracts": 1500,
             "trades": 800, "improvement": -.0594, "net_per_contract": -.038,
             "significant": False},
            {"model": "digital-gbm-ewma-v1", "label": "GBM + EWMA", "contracts": 297,
             "trades": 150, "improvement": -.0232, "net_per_contract": -.036,
             "significant": False},
            {"model": "queue-pressure-v1", "label": "Queue pressure", "contracts": 297,
             "trades": 150, "improvement": -.0214, "net_per_contract": -.034,
             "significant": False},
            {"model": "jump-diffusion-v1", "label": "Jump diffusion", "contracts": 296,
             "trades": 150, "improvement": -.0239, "net_per_contract": -.034,
             "significant": False},
            {"model": "digital-odds-lag-logistic-v1", "label": "Odds-lag learner",
             "contracts": 63, "trades": 30, "improvement": -.0058,
             "net_per_contract": -.043, "significant": False},
        ],
        "benefit": {"status": "not_improving", "targets": 3263, "days": 12},
        "cycles": {
            "forecast": {"status": "recorded", "checked_at": NOW, "issued": 20,
                         "expired": 0},
            "learning": {"status": "recorded", "checked_at": NOW, "math_ok": True,
                         "model_review_ok": True, "issued": 5, "settled": 4},
        },
        "coverage": {"cycles": 279, "expired": 458, "issued": 9000},
        "feeds": [{"asset": "BTC", "venue": "hyperliquid", "status": "ok",
                   "last_success_at": NOW.isoformat(),
                   "last_candle_close": (NOW - timedelta(minutes=5)).isoformat()}],
        "intelligence": {
            "latest": {"issued_at": (NOW - timedelta(days=4)).isoformat(),
                       "stances": {5: ("—", "none")}},
            "report": {"followed": 95, "followed_hit_rate": .54, "abstained": 2},
        },
    }


def _embeds():
    return home_module.performance_embeds("BTC", 5, _reports(), now=NOW,
                                          venues=("hyperliquid",))


def test_three_embeds_carry_every_number_from_the_old_card():
    embeds = _embeds()
    assert len(embeds) == 3
    assert embeds[0].title == "📊 Bitcoin · 5-min · Bot performance"
    assert embeds[1].title == "🧪 Models on trial here (8) — Bitcoin · 5-min"
    assert embeds[2].title == "🎲 Polymarket contest"
    assert "Research only" in embeds[2].footer.text
    assert "Per-model detail: Detailed Research" in embeds[2].footer.text
    fields = _all_fields(embeds)

    # Embed 1: verdict first, then one fact per line, label first.
    assert embeds[0].description.startswith(
        "**No edge yet: Polymarket's odds are still more accurate.**")
    assert "predictions saved before the result was known" in embeds[0].description
    assert "Does it make money? Not proven yet" in embeds[0].description
    score = fields["🎯 Scoreboard (last 7 days)"]
    assert "🎯 **vs Polymarket odds — behind**" in score
    assert "Error 0.233 vs 0.231 · 2,001 contracts" in score
    assert "3,408 past contracts studied (history, not live predictions)" in score
    assert "🗄️ **6-year model vs odds — behind**" in score
    assert "0.250 vs 0.230 · 1,938 contracts" in score
    assert "Its bets won 344/1023, -12.1¢ per $1 after fees" in score
    assert "📈 **Up/down calls** — right **53.0%**" in score
    assert "(8,820 of 16,646) · vs a coin flip: behind (0.251 vs 0.250)" in score
    assert "16,912 of 16,921 predictions checked · 9 exchanges · main model only" in score
    confidence = fields["🎚️ Can you trust its confidence?"]
    assert confidence.startswith(
        "⚠️ **Overconfident:** it says 55% on average but is right only 53%.")
    for calls in ("8,977", "5,427", "1,750", "492"):
        assert calls in confidence
    conditions = fields["📊 Where it's right"]
    for row in ("Quiet", "Normal", "Turbulent", "67.0%", "+19.2", "206"):
        assert row in conditions
    assert "Best in **turbulent** markets" in conditions

    # Embed 2: every trial row, grouped under plain headings.
    trials = fields["🧪 Models on trial here (8)"]
    for name in ("6-year", "Calibrated", "Fast trend", "Trend learn", "Trend-only",
                 "Cross-all", "Naive Bayes", "Nearest-21"):
        assert name in trials
    for heading in ("▲ Ahead of the main model", "= Level with the main model",
                    "⏳ Too early to judge", "▼ Behind the main model"):
        assert heading in trials
    assert ("Closest to passing: **6-year** (ahead on 1 of 6 exchanges, ahead after fees). "
            "Needs 13 more days before it can be trusted.") in embeds[1].description
    how = fields["How to read this table"]
    assert "Day = days of results" in how and "Res = graded results" in how
    assert "vol = volatility" in how and "learning speed, not accuracy" in how
    assert "All markets: 115 on trial · 0 passed · 102 cut early for losing" in how
    best = fields["🏆 Best model per timeframe (BTC)"]
    assert "**5m** · **Trend learn** +7.1 edge · 140 results over 3 days, not proven yet" in best
    assert "**15m** · **6-year** +8.0 edge · 696 results over 8 days, not proven yet" in best
    assert "**60m** · **6-year** +23.4 edge · 171 results over 8 days, not proven yet" in best
    lab = fields["🧬 Model Lab"]
    assert "12 designs testing, 0 closed so far" in lab
    assert "**Design unavailable** on 15-min (early, not proven)" in lab
    assert "Lab 2e87c2" not in lab

    # Embed 3: the contest verdict, all six rows, then health.
    contest = fields["🎲 Polymarket contest"]
    assert contest.startswith("▼ **All 6 are behind Polymarket's own odds.**")
    for name in ("Order book", "Pooled 5ee9", "Formula", "Pressure", "Jump model",
                 "Odds-lag"):
        assert name in contest
    assert "To qualify: 300 contracts and a statistical pass" in contest
    health = fields["⚙️ Running and learning"]
    assert "✅ grading/review just now" in health and "✅ predictions just now" in health
    assert "✅ 1/1 exchanges live" in health
    assert "⚠️ 279/288 cycles in 24h, 458 predictions skipped (ran late)" in health
    assert ("**Getting more accurate?** no, not beating the coin flip "
            "(3,263 results over 12 days)") in health
    assert ("**AI reviewer:** automatic reviews are paused (last one 4 days ago); "
            "it still answers when asked") in health
    assert "agreed 95 times (right 54%), sat out 2" in health


def test_three_embeds_fit_discord_limits_with_no_mid_word_cut():
    embeds = _embeds()
    for embed in embeds:
        assert len(embed) <= 6000
        for field in embed.fields:
            assert len(field.value) <= 1024
            assert "…" not in field.value   # nothing was truncated to fit
            for line in field.value.splitlines():
                if line.startswith(("```", "Model", "It said", "Market")):
                    assert len(line) <= 38
    assert sum(len(e) for e in embeds) <= 5900
    # Tables stay one line per model on a phone.
    for embed in embeds:
        for field in embed.fields:
            for block in field.value.split("```")[1::2]:
                assert max(len(line) for line in block.splitlines()) <= 35


def test_a_stale_trials_read_marks_only_the_trials_embed():
    reports = _reports()
    reports["_stale"] = {"trials": timedelta(minutes=30)}
    embeds = home_module.performance_embeds("BTC", 5, reports, now=NOW,
                                            venues=("hyperliquid",))
    assert embeds[0].description.startswith("**⚠️ NOT ALL LIVE")
    assert not any(f.name.endswith("⏳ OLD COPY") for f in embeds[0].fields)
    trials_field = next(f for f in embeds[1].fields
                        if f.name.startswith("🧪 Models on trial here"))
    assert trials_field.name.endswith("⏳ OLD COPY")
    assert trials_field.value.startswith("**⏳ Old copy from 30 min ago, not live.**")
    assert not any(f.name.endswith("OLD COPY") for f in embeds[2].fields)


def test_no_contest_means_the_third_embed_is_just_health():
    reports = _reports()
    del reports["contest"]
    embeds = home_module.performance_embeds("BTC", 5, reports, now=NOW,
                                            venues=("hyperliquid",))
    assert embeds[2].title == "⚙️ Running and learning"
    assert "🎲 Polymarket contest" not in _all_fields(embeds)
    assert "⚙️ Running and learning" in _all_fields(embeds)


@pytest.mark.asyncio
async def test_render_embeds_sends_three_embeds_for_the_performance_page(
        tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    try:
        embeds = await home.render_embeds()
        assert len(embeds) == 3
        assert embeds[0].title == "📊 Bitcoin · 5-min · Bot performance"
        assert "🎯 Scoreboard (last 7 days)" in _all_fields(embeds)
        assert "⚙️ Running and learning" in _all_fields(embeds)
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_render_embeds_still_sends_one_embed_for_other_pages(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    try:
        home.navigate("help")
        embeds = await home.render_embeds()
        assert len(embeds) == 1 and embeds[0].title == "❓ Getting Started"
    finally:
        await _close(bot, home)


def test_full_performance_card_uses_the_saved_lab_design_name(monkeypatch):
    from parlay_bot.perpetuals import model_lab

    family = model_lab.family_of(model_lab.SEEDS[0], 15)
    monkeypatch.setattr(model_lab, "_GENOMES", {
        family: model_lab.canonical(model_lab.SEEDS[0], 15)})
    reports = _reports()
    reports["best"]["lab"]["15"]["best"]["family"] = family
    embeds = home_module.performance_embeds("BTC", 5, reports, now=NOW, venues=("hyperliquid",))
    lab = _all_fields(embeds)["🧬 Model Lab"]
    assert "**Price+vol+volume+RSI/MACD · steady** on 15-min" in lab
    assert "Lab " + family.rsplit("_", 1)[-1][:6] not in lab
