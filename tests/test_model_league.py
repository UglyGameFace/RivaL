"""Per-market model contest: market price is the benchmark; Holm-corrected promotion."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.polymarket_live_board import live_polymarket_embed
from parlay_bot.perpetuals.model_league import (
    BOOK,
    MIN_CONTRACTS,
    holm,
    league,
    league_text,
)
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


def test_holm_is_stricter_than_testing_each_market_alone():
    # 0.03 alone would pass at 5%; among 12 tests Holm needs 0.05/12 for the first.
    assert holm({"a": 0.03, "b": 0.5}) == set()
    assert holm({"a": 0.001, "b": 0.02}) == {"a", "b"}
    assert holm({f"m{i}": 0.004 for i in range(12)}) == {f"m{i}" for i in range(12)}
    assert holm({f"m{i}": 0.0045 for i in range(12)}) == set()


def _seed(store, *, n, model_good):
    rng = random.Random(7)
    with store.connect() as conn:
        for i in range(n):
            closes = NOW - timedelta(minutes=5 * (i + 1))
            slug = f"btc-updown-5m-{i}"
            truth = rng.random()
            up = rng.random() < truth
            market = 0.5  # an uninformed price
            model = truth if model_good else 0.5 + (0.5 - truth) * 0.6
            conn.execute(
                "INSERT INTO rival_polymarket_observations (slug,condition_id,asset,horizon,"
                "resolution_source,expires_at,observed_at,features_json,evidence_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (slug, "0x" + "a" * 64, "BTC", 5, "chainlink", closes.isoformat(),
                 (closes - timedelta(minutes=5)).isoformat(), "[]", "e" * 64))
            conn.execute(
                "INSERT INTO rival_polymarket_model_forward (slug,model_version,issued_at,"
                "weights_sha256,probability_up,market_baseline) VALUES (?,?,?,?,?,?)",
                (slug, BOOK, (closes - timedelta(minutes=4)).isoformat(), "w" * 64,
                 model, market))
            conn.execute(
                "INSERT INTO rival_polymarket_ai_outcomes (slug,condition_id,direction,"
                "resolved_at,observed_at,receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?)",
                (slug, "0x" + "a" * 64, "UP" if up else "DOWN", closes.isoformat(),
                 closes.isoformat(), "r" * 64, "{}"))


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "league.sqlite")
    PolymarketOutcomeLearner(store).initialize()
    PolymarketResearch(store).initialize()
    return store


def test_a_model_that_truly_beats_the_price_becomes_champion(tmp_path):
    store = _store(tmp_path)
    try:
        _seed(store, n=MIN_CONTRACTS + 50, model_good=True)
        entry = league(store, now=NOW)["markets"][("BTC", 5)]
        assert entry["champion"] == BOOK
        assert "beats Polymarket's price" in league_text(entry, asset="BTC", horizon=5)
    finally:
        store.close()


def test_a_worse_model_never_leads_and_the_card_says_so(tmp_path):
    store = _store(tmp_path)
    try:
        _seed(store, n=MIN_CONTRACTS + 50, model_good=False)
        entry = league(store, now=NOW)["markets"][("BTC", 5)]
        assert entry["champion"] is None
        text = league_text(entry, asset="BTC", horizon=5)
        assert "No model beats Polymarket's price for BTC 5m" in text and "worse" in text
        embed = live_polymarket_embed(None, None, asset="BTC", horizon=5, league=entry)
        assert isinstance(embed, discord.Embed)
        assert any(f.name == "🏆 Best model for this market" for f in embed.fields)
    finally:
        store.close()


def test_too_few_contracts_can_never_promote(tmp_path):
    store = _store(tmp_path)
    try:
        _seed(store, n=50, model_good=True)
        assert league(store, now=NOW)["markets"][("BTC", 5)]["champion"] is None
    finally:
        store.close()
