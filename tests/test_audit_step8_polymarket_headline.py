"""Audit Step 8 (2026-11-10): Polymarket result is the Performance headline.

The "RivaL vs Polymarket's odds, after fees" comparison comes FIRST on the
Performance page; the next-candle direction sections stay below it as
supporting research, in their existing order relative to each other.
Nothing is deleted: the field set is unchanged, only reordered. The
Polymarket field leads with a verdict-first headline (Step 2 style:
verdict line first, <=12 words), then the reason (the error scores),
then the sample size — using only numbers the page already computes.
"""
from __future__ import annotations

import pytest

from tests.test_performance_overview import (
    _close,
    _fields,
    _live_record,
    _make_home,
    _patch_live_reads,
)

POLY = "🎯 Polymarket / Kalshi predictions"
EXCHANGE = "📈 BTC exchange predictions (up or down?)"
LEARNING = "🧠 Is it learning?"
RUNNING = "📡 Is everything running?"


def _headline(field_value: str) -> str:
    return field_value.split("\n")[0]


@pytest.mark.asyncio
async def test_polymarket_field_is_first_and_nothing_is_lost(tmp_path, monkeypatch):
    """With both live learners issued: Polymarket first, then the exchange
    field, then the learner fields in their old relative order, then
    learning and running. Same six fields as before Step 8 — reordered,
    not reduced."""
    bot, home = _make_home(tmp_path)
    live = _live_record(issued=12, decided=9, hits=6, windows=11)
    fast = _live_record(issued=12, decided=9, hits=5, windows=11, brier=.248,
                        gain_v1=.003, updates=93, drift=.021)
    _patch_live_reads(
        monkeypatch,
        pooled={"issued": 12761, "graded": 12761, "decided": 12761, "hits": 6698,
                "windows": 1527, "model_brier": .249, "incumbent_brier": .251,
                "baseline_brier": .250},
        regime={"issued": 935, "graded": 935, "decided": 935, "hits": 511,
                "windows": 133, "model_brier": .253, "incumbent_brier": .253,
                "baseline_brier": .250, "selection_effect": .001, "coverage": .06},
        live=live, live_fast=fast,
    )
    try:
        fields = _fields(await home.render())
        assert list(fields) == [
            POLY,
            EXCHANGE,
            "🔬 Trend learner",
            "🔬 Fast trend learner",
            LEARNING,
            RUNNING,
        ]
        # Next-candle direction is supporting research now, not gone:
        # the trial rows still live inside the exchange field.
        assert "**6-year history model:**" in fields[EXCHANGE]
        assert "**trend-only test:**" in fields[EXCHANGE]
        assert "Getting more accurate?**" in fields[LEARNING]
        assert "Live market feeds:**" in fields[RUNNING]
        assert max(map(len, fields.values())) <= 1024
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_polymarket_field_is_first_without_live_learners(tmp_path):
    """No live learner issued yet: four fields, Polymarket first."""
    bot, home = _make_home(tmp_path)
    try:
        fields = _fields(await home.render())
        assert list(fields) == [POLY, EXCHANGE, LEARNING, RUNNING]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headline_verdict_first_when_trailing_polymarket(tmp_path):
    """Default fixture: RivaL 0.239 vs Polymarket 0.222 — the headline is
    the verdict, then the reason (error scores), then the sample size."""
    bot, home = _make_home(tmp_path)
    try:
        poly = _fields(await home.render())[POLY]
        lines = poly.split("\n")
        assert lines[0] == "**No, not yet — RivaL trails Polymarket's odds.**"
        assert lines[1].startswith(
            "**Better than Polymarket's own odds?** No, not yet. "
            "(Error score: RivaL 0.239, Polymarket 0.222; lower is better.)"
        )
        assert lines[2] == "**Checked:** 300 live predictions (last 7 days)"
        assert "Past results studied:** 402 (history, not live predictions)" in poly
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headline_verdict_first_when_beating_polymarket(tmp_path):
    """RivaL 0.210 vs Polymarket 0.222 — the headline says so, first."""
    bot, home = _make_home(tmp_path)
    home.polymarket_status = lambda *_: {
        "settled_forward_contracts": 300,
        "learned_contracts": 402,
        "score_window_days": 7,
        "forward_brier": .2100,
        "market_baseline_brier": .2222,
    }
    try:
        poly = _fields(await home.render())[POLY]
        lines = poly.split("\n")
        assert lines[0] == "**Yes, so far — RivaL beats Polymarket's odds.**"
        assert lines[1].startswith(
            "**Better than Polymarket's own odds?** Slightly, so far, "
            "but not proven enough to trade on."
        )
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headline_verdict_first_when_too_early(tmp_path):
    """Nothing settled yet: the headline is Not yet, and the old
    too-early wording survives below it."""
    bot, home = _make_home(tmp_path)
    home.polymarket_status = lambda *_: {
        "observed_contracts": 99,
        "learned_contracts": 99,
        "settled_forward_contracts": 0,
        "forward_brier": None,
        "market_baseline_brier": None,
    }
    try:
        poly = _fields(await home.render())[POLY]
        lines = poly.split("\n")
        assert lines[0] == "**Not yet — can't compare with Polymarket's odds yet.**"
        assert "too early to compare with Polymarket's odds" in poly
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_after_fees_line_stays_in_headline_field(tmp_path):
    """The after-fees evidence (pooled disagreements, ¢ per $1 after
    Polymarket's fee) stays in the headline field, below the verdict."""
    bot, home = _make_home(tmp_path)
    home.polymarket_status = lambda *_: {
        "settled_forward_contracts": 300,
        "learned_contracts": 402,
        "score_window_days": 7,
        "forward_brier": .2387,
        "market_baseline_brier": .2222,
        "pooled": {
            "contracts": 815,
            "model_brier": .252,
            "market_brier": .229,
            "real_value_bets": 10,
            "real_value_bet_wins": 3,
            "real_return_per_dollar_after_fees": -0.125,
        },
    }
    try:
        fields = _fields(await home.render())
        assert next(iter(fields)) == POLY
        assert "after Polymarket's fee" in fields[POLY]
        assert "lost 12.5¢ per $1" in fields[POLY]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headlines_are_twelve_words_or_fewer(tmp_path):
    """Step 2 pin: every headline variant is a verdict line of at most
    12 words."""
    variants = [
        {"settled_forward_contracts": 300, "learned_contracts": 402,
         "score_window_days": 7, "forward_brier": .2387,
         "market_baseline_brier": .2222},
        {"settled_forward_contracts": 300, "learned_contracts": 402,
         "score_window_days": 7, "forward_brier": .2100,
         "market_baseline_brier": .2222},
        {"settled_forward_contracts": 0, "learned_contracts": 99,
         "score_window_days": 7, "forward_brier": None,
         "market_baseline_brier": None},
    ]
    for status in variants:
        bot, home = _make_home(tmp_path)
        home.polymarket_status = lambda *_, _s=status: _s
        try:
            headline = _headline(_fields(await home.render())[POLY])
            words = headline.strip("*").split()
            assert len(words) <= 12, headline
            assert words[0] in ("Yes,", "No,", "Not")
        finally:
            await _close(bot, home)
