"""The Up/Down price formula and its locked scorecard."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.coinbase_reference import CoinbaseReference
from parlay_bot.perpetuals.polymarket_formula_test import (
    MIN_BETS,
    FormulaTest,
    formula_record,
    formula_text,
)
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.perpetuals.price_formula import chance_up, per_second_sigma
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 7, tzinfo=UTC)


@dataclass(frozen=True)
class Notice:
    slug: str
    asset: str = "BTC"
    horizon: int = 5
    issued: datetime = NOW - timedelta(minutes=1)
    expires: datetime = NOW + timedelta(minutes=4)
    up_ask: float = 0.50
    down_ask: float = 0.51
    quote_at: datetime = NOW
    condition_id: str = "0x" + "f" * 64


def test_formula_is_the_binary_option_chance():
    assert chance_up(100.0, 100.0, 0.001, 60) == 0.5
    sigma, seconds = 0.0002, 100.0
    two_sd_up = 100.0 * math.exp(2 * sigma * math.sqrt(seconds))
    assert abs(chance_up(two_sd_up, 100.0, sigma, seconds) - 0.97725) < 1e-4
    assert abs(chance_up(100.0 / (two_sd_up / 100.0), 100.0, sigma, seconds) - 0.02275) < 1e-4
    assert chance_up(100.0, 100.0, 0.0, 60) is None
    flat = [100.0] * 30
    assert per_second_sigma(flat) is None  # no movement: no volatility estimate
    wiggle = [100.0 * (1.001 if i % 2 else 1.0) for i in range(61)]
    assert 0 < per_second_sigma(wiggle) < 0.001


def test_open_price_is_the_contract_minutes_opening_trade():
    rows = [(int((NOW - timedelta(minutes=2)).timestamp()), 99.0, 99.5),
            (int((NOW - timedelta(minutes=1)).timestamp()), 100.0, 101.0)]
    assert CoinbaseReference.open_at(rows, NOW - timedelta(minutes=1)) == 100.0
    assert CoinbaseReference.open_at(rows, NOW - timedelta(minutes=5)) is None


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "formula.sqlite")
    PolymarketOutcomeLearner(store).initialize()
    PolymarketResearch(store).initialize()
    return store


def _settle(store, notice, direction):
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes (slug,condition_id,direction,resolved_at,"
            "observed_at,receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?)",
            (notice.slug, notice.condition_id, direction, notice.expires.isoformat(),
             (notice.expires + timedelta(minutes=1)).isoformat(), "r" * 64, "{}"))


def test_first_qualifying_moment_is_frozen_and_scored_at_the_ask(tmp_path):
    store = _store(tmp_path)
    try:
        test = FormulaTest(store)
        notice = Notice(slug="btc-updown-5m-1")
        # Price at the open: 50/50, no bet, but the contract is counted.
        flat = test.consider(notice, price=100.0, open_price=100.0,
                             sigma_per_second=0.0002, now=NOW)
        assert flat["bet"] is None and abs(flat["chance_up"] - 0.5) < 1e-9
        # Price jumps: UP is far more likely than its 50¢ ask -> first bet frozen.
        up = test.consider(notice, price=100.5, open_price=100.0,
                           sigma_per_second=0.0002, now=NOW + timedelta(seconds=10))
        assert up["bet"]["side"] == "UP"
        # A later, different reading can never replace the frozen bet.
        later = Notice(slug="btc-updown-5m-1", up_ask=0.10, down_ask=0.91,
                       quote_at=NOW + timedelta(seconds=20))
        test.consider(later, price=99.0, open_price=100.0,
                      sigma_per_second=0.0002, now=NOW + timedelta(seconds=20))
        with store.connect() as conn:
            rows = conn.execute("SELECT side, ask FROM rival_polymarket_formula_bets").fetchall()
        assert [(r["side"], r["ask"]) for r in rows] == [("UP", 0.50)]
        # Under 90 s left: never a new bet.
        late = Notice(slug="btc-updown-5m-2", expires=NOW + timedelta(seconds=60))
        assert test.consider(late, price=101.0, open_price=100.0,
                             sigma_per_second=0.0002, now=NOW) is None
        _settle(store, notice, "DOWN")
        record = formula_record(store, asset="BTC", horizon=5, now=NOW + timedelta(hours=1))
        assert record["bets"] == 1 and record["wins"] == 0 and record["contracts"] == 1
        assert record["return_per_dollar"] == -1.0 and not record["unlocked"]
        assert "LOCKED" in formula_text(record) and str(MIN_BETS)[:1] in formula_text(record)
    finally:
        store.close()
