"""Round-5 batch 9 (N7/N8) pins.

(i) Served crypto probabilities are disclosed as raw, uncalibrated model
output at the serving/render sites (signals + opportunities embeds).
(ii) The sports model does not use live odds: identical pregame events
that differ only in their odds payload issue identical probabilities,
and the user-facing scope text says so.
(iii) Qwen is gone from the LLM catalog: the gateway serves LFM3 only
and rejects a Qwen profile.
"""
from __future__ import annotations

import json
import runpy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.home import SPORTS_MODEL_SCOPE_TEXT
from parlay_bot.discord_app.opportunities import crypto_desk_embed
from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import BookQuote, PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.research.sports_results import (
    SportsResultStore,
    model_probabilities,
)
from parlay_bot.storage.hot import SQLiteHotStore

ROOT = Path(__file__).resolve().parents[1]
LLM_ROOT = ROOT / "services" / "rival-llm"


# --- shared crypto seed scaffolding (mirrors test_rival_signal_decisions) ---

def _seed_prediction(market, now, *, venue, probability, expires_in=420):
    market.initialize()
    symbols = {
        "hyperliquid": ("BTC", "USDC"),
        "binance": ("BTCUSDT", "USDT"),
    }
    contract, quote = symbols[venue]
    opened = now - timedelta(minutes=6)
    closed = now - timedelta(minutes=1)
    candle = MarketCandle(
        venue, contract, quote, 5, opened, closed,
        closed + timedelta(milliseconds=1),
        80999, 81005, 80991, 81001, 20.0, 42,
    )
    market.ingest(MarketBatch(
        (), (candle,), 5, closed + timedelta(milliseconds=1),
    ))
    market.record_feed_health(
        venue=venue, asset="BTC", status="ok", attempted_at=now,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    ForwardAccuracyMonitor(market).initialize()
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, contract, quote, "BTC", 5, "prospective-logistic-persistent-v3",
                opened.isoformat(), 81001,
                (now - timedelta(seconds=30)).isoformat(),
                (now + timedelta(seconds=expires_in - 300)).isoformat(),
                probability, .5, 120,
            ),
        )


def _seed_settled(market, now, *, venue, count=50, correct=30):
    ForwardAccuracyMonitor(market).initialize()
    with market.store.connect() as conn:
        for i in range(count):
            issued = (now - timedelta(days=6)
                      + timedelta(days=i % 6, minutes=i))
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated,
                    settled_at,outcome_up,model_brier,baseline_brier)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    venue, "BTC", "USDC", "BTC", 5,
                    "prospective-logistic-persistent-v3",
                    (issued - timedelta(minutes=5)).isoformat(), 81001,
                    issued.isoformat(),
                    (issued + timedelta(minutes=5)).isoformat(),
                    .7, .5, 120,
                    (issued + timedelta(minutes=10)).isoformat(),
                    1 if i < correct else 0, .20, .25,
                ),
            )


def _mock_quotes(now):
    hyper = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=81000, ask=81001,
        bid_quantity=10, ask_quantity=10, exchange_at=now, received_at=now,
    )
    binance = BookQuote(
        asset="BTC", venue="binance_usdm", symbol="BTCUSDT",
        bid=81003, ask=81004, bid_quantity=9, ask_quantity=9,
        exchange_at=now, received_at=now,
    )
    return type("Quotes", (), {
        "get_reference": AsyncMock(return_value=hyper),
        "get": AsyncMock(return_value=binance),
    })()


# --- (i) calibration disclosure at the serving/render sites ---

@pytest.mark.asyncio
async def test_signal_embed_labels_weak_probability_raw_uncalibrated(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "weak.sqlite")
    market = PerpetualMarketStore(store)
    try:
        _seed_prediction(market, now, venue="hyperliquid", probability=.514)
        report = await signal_embed(market, _mock_quotes(now), asset="BTC",
                                    horizon_minutes=5, now=now)
        field = next(x.value for x in report.fields
                     if x.name == "⚡ How sure the model is")
        assert "51.4%" in field
        assert "raw, uncalibrated" in field
        assert "calibrated" in field.lower()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_signal_embed_labels_watch_probability_raw_uncalibrated(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "watch.sqlite")
    market = PerpetualMarketStore(store)
    try:
        _seed_prediction(market, now, venue="hyperliquid", probability=.61)
        _seed_settled(market, now, venue="hyperliquid")
        signal = await signal_embed(market, _mock_quotes(now), asset="BTC",
                                    horizon_minutes=5, now=now)
        assert "Leans UP: watch only" in signal.title
        details = next(x.value for x in signal.fields if x.name == "Details")
        assert "raw, uncalibrated" in details
        strength = next(x.value for x in signal.fields
                        if x.name == "⚡ How sure the model is")
        assert "raw, uncalibrated" in strength
    finally:
        store.close()


def test_crypto_desk_embed_labels_probability_raw_uncalibrated(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "desk.sqlite")
    market = PerpetualMarketStore(store)
    try:
        _seed_prediction(market, now, venue="hyperliquid", probability=.61)
        embed = crypto_desk_embed(market, asset="BTC", now=now)
        watch = next(x for x in embed.fields
                     if x.name.startswith("👀 Leans"))
        assert "61.0%" in watch.value
        assert "raw, uncalibrated" in watch.value
    finally:
        store.close()


# --- (ii) sports odds are not a model input ---

def _final_event(*, event_id, day, hour, home_id, away_id, home_score, away_score):
    start = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day, hours=hour)
    return {
        "eventID": event_id, "sportID": "SOCCER", "leagueID": "EPL",
        "status": {
            "finalized": True, "cancelled": False,
            "startsAt": start.isoformat(),
        },
        "teams": {
            "home": {"teamID": home_id},
            "away": {"teamID": away_id},
        },
        "scores": {"home": home_score, "away": away_score},
        "results": {
            "reg": {
                "home": {"points": home_score},
                "away": {"points": away_score},
            },
        },
    }


def _seed_qualifying_league(research, at):
    # Six teams, lower index always wins 3-0: outcomes are strongly
    # team-dependent, so the team-aware model beats the flat league
    # baseline by more than the 0.01 Brier gate.
    teams = [f"team-{i}" for i in range(6)]
    game = 0
    # 140 games over 35 days (4/day): the chronological holdout (games
    # 80..139) then spans 15 dates, clearing the 14-date gate.
    for day in range(35):
        for slot in range(4):
            home = teams[game % 6]
            away = teams[(game + 1 + slot) % 6]
            if home == away:
                away = teams[(game + 2) % 6]
            home_wins = teams.index(home) < teams.index(away)
            event = _final_event(
                event_id=f"seed-{game}", day=day, hour=slot * 4,
                home_id=home, away_id=away,
                home_score=3 if home_wins else 0,
                away_score=0 if home_wins else 3,
            )
            assert research.save_finalized(
                event, observed_at=datetime(2026, 9, 1, tzinfo=UTC)
                + timedelta(days=day, hours=slot * 4 + 1),
            ) == "inserted"
            game += 1
    assert research.evaluate(
        sport="SOCCER", league="EPL",
    ).status == "HISTORICAL_SHADOW"


def _pregame_event(*, event_id, kickoff, odds=None):
    event = {
        "eventID": event_id, "sportID": "SOCCER", "leagueID": "EPL",
        "status": {
            "started": False, "cancelled": False, "finalized": False,
            "startsAt": kickoff.isoformat(),
        },
        "teams": {
            "home": {"teamID": "team-0"},
            "away": {"teamID": "team-5"},
        },
    }
    if odds is not None:
        event["odds"] = odds
    return event


def test_sports_forecast_ignores_live_odds(tmp_path):
    store = SQLiteHotStore(tmp_path / "odds.sqlite")
    research = SportsResultStore(store)
    research.initialize()
    now = datetime(2026, 10, 10, tzinfo=UTC)
    kickoff = now + timedelta(days=2)
    try:
        _seed_qualifying_league(research, now)
        no_odds = _pregame_event(event_id="game-no-odds", kickoff=kickoff)
        assert research.issue_forecast(no_odds, observed_at=now, now=now) == "issued"
        favourite = _pregame_event(
            event_id="game-fav", kickoff=kickoff,
            odds={"home": 1.20, "draw": 7.50, "away": 15.00,
                  "books": ["book-a", "book-b"]},
        )
        assert research.issue_forecast(favourite, observed_at=now, now=now) == "issued"
        outsider = _pregame_event(
            event_id="game-out", kickoff=kickoff,
            odds={"home": 9.00, "draw": 5.00, "away": 1.30,
                  "books": ["book-a"]},
        )
        assert research.issue_forecast(outsider, observed_at=now, now=now) == "issued"
        with store.connect() as conn:
            rows = {
                row["event_id"]: json.loads(row["probability_json"])
                for row in conn.execute(
                    "SELECT event_id, probability_json FROM rival_sport_forecasts"
                ).fetchall()
            }
        assert rows["game-no-odds"] == rows["game-fav"] == rows["game-out"]
    finally:
        store.close()


def test_sports_scope_text_discloses_odds_not_used():
    assert "does not use live odds" in SPORTS_MODEL_SCOPE_TEXT
    assert "past final scores only" in SPORTS_MODEL_SCOPE_TEXT


def test_sports_model_docstring_discloses_odds_not_an_input():
    assert "odds are not an input" in (model_probabilities.__doc__ or "").lower()


# --- (iii) Qwen is gone from the catalog ---

def _load_service():
    sys.path.insert(0, str(LLM_ROOT))
    sys.path.insert(0, str(ROOT / "src" / "parlay_bot" / "perpetuals"))
    return runpy.run_path(str(LLM_ROOT / "service.py"))


def test_llm_catalog_is_lfm3_only():
    service = _load_service()
    assert service["MODEL_CATALOG"] == {
        "lfm3": ("LiquidAI/LFM3-1.7B-GGUF", "LFM3-1.7B-Q4_K_M.gguf"),
    }
    assert "qwen" not in (LLM_ROOT / "service.py").read_text().lower()


def test_llm_gateway_rejects_qwen_profile():
    service = _load_service()
    with pytest.raises(ValueError, match="Unrecognized model profile"):
        service["Gateway"](
            "127.0.0.1", 0, token="<redacted>" * 48, model="qwen2b",
        )


def test_benchmark_no_longer_claims_a_qwen_comparison():
    text = (LLM_ROOT / "benchmark.py").read_text()
    assert "LFM3 vs Qwen" not in text
    assert "was never run" in text
