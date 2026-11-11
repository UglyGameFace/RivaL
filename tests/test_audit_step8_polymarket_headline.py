"""Audit Step 8 (2026-11-10): the Polymarket result is the Performance headline.

The verdict against Polymarket's own odds is the first line of the page
(the embed description), at most 12 words, and the comparison it rests on
(error scores and the number of contracts) is the first line of the
scoreboard. The after-fees evidence stays on the scoreboard below it.
"""
from __future__ import annotations

import pytest

from tests.test_performance_overview import SCORE, _close, _fields, _make_home


def _headline(panel) -> str:
    return panel.description.split("\n")[0]


@pytest.mark.asyncio
async def test_headline_verdict_first_when_trailing_polymarket(tmp_path):
    """Default fixture: RivaL 0.239 vs Polymarket 0.222."""
    bot, home = _make_home(tmp_path)
    try:
        panel = await home.render()
        assert _headline(panel) == "**No edge yet: Polymarket's odds are still more accurate.**"
        assert _fields(panel)[SCORE].split("\n")[0] == (
            "🎯 **vs Polymarket odds:** behind · error 0.239 vs 0.222 · 300 contracts")
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headline_verdict_first_when_beating_polymarket(tmp_path):
    bot, home = _make_home(tmp_path)
    home.polymarket_status = lambda *_: {
        "settled_forward_contracts": 300, "learned_contracts": 402,
        "score_window_days": 7, "forward_brier": .2100, "market_baseline_brier": .2222,
    }
    try:
        panel = await home.render()
        assert _headline(panel) == "**Ahead of Polymarket's odds so far, but not proven yet.**"
        assert "**vs Polymarket odds:** ahead · error 0.210 vs 0.222" in _fields(panel)[SCORE]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headline_verdict_first_when_too_early(tmp_path):
    bot, home = _make_home(tmp_path)
    home.polymarket_status = lambda *_: {
        "observed_contracts": 99, "learned_contracts": 99,
        "settled_forward_contracts": 0, "forward_brier": None, "market_baseline_brier": None,
    }
    try:
        panel = await home.render()
        assert _headline(panel) == "**Too early to compare with Polymarket's odds.**"
        assert "0 contracts so far, too few" in _fields(panel)[SCORE]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_a_failed_polymarket_read_is_never_a_verdict(tmp_path):
    bot, home = _make_home(tmp_path)

    def broken(*_):
        raise RuntimeError("down")

    home.polymarket_status = broken
    try:
        panel = await home.render()
        assert _headline(panel).startswith("**Couldn't load the Polymarket scores right now.**")
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_after_fees_line_stays_on_the_scoreboard(tmp_path):
    bot, home = _make_home(tmp_path)
    home.polymarket_status = lambda *_: {
        "settled_forward_contracts": 300, "learned_contracts": 402,
        "score_window_days": 7, "forward_brier": .2387, "market_baseline_brier": .2222,
        "pooled": {"contracts": 815, "model_brier": .252, "market_brier": .229,
                   "real_value_bets": 10, "real_value_bet_wins": 3,
                   "real_return_per_dollar_after_fees": -0.125},
    }
    try:
        score = _fields(await home.render())[SCORE]
        assert ("**6-year model vs odds:** behind · 0.252 vs 0.229 · 815 contracts · "
                "its bets won 3/10, -12.5¢ per $1 after fees") in score
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_headlines_are_twelve_words_or_fewer(tmp_path):
    variants = [
        {"settled_forward_contracts": 300, "forward_brier": .2387,
         "market_baseline_brier": .2222},
        {"settled_forward_contracts": 300, "forward_brier": .2100,
         "market_baseline_brier": .2222},
        {"settled_forward_contracts": 0, "forward_brier": None,
         "market_baseline_brier": None},
    ]
    for status in variants:
        bot, home = _make_home(tmp_path)
        home.polymarket_status = lambda *_, _s=status: _s
        try:
            headline = _headline(await home.render())
            assert len(headline.strip("*").split()) <= 12, headline
        finally:
            await _close(bot, home)
