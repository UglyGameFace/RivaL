"""Start-of-contract calls: one per contract, priced at the open, locked until proven."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from parlay_bot.discord_app.polymarket_live_board import value_line
from parlay_bot.perpetuals.early_call import (
    MIN_BETS,
    current_early_call,
    early_call_record,
    early_call_text,
    implied_bet,
)
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_research import ContractCall, PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 6, tzinfo=UTC)


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "early.sqlite")
    PolymarketOutcomeLearner(store).initialize()
    PolymarketResearch(store).initialize()
    return store


def _contract(conn, i, *, p, outcome, family="a", up=0.50, down=0.51, settled=True,
              closes=None):
    closes = closes or NOW - timedelta(minutes=5 * (i + 1))
    slug = f"btc-updown-5m-{i}"
    conn.execute(
        "INSERT INTO rival_polymarket_observations (slug,condition_id,asset,horizon,"
        "resolution_source,expires_at,observed_at,features_json,evidence_sha256) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        (slug, "0x" + "d" * 64, "BTC", 5, "chainlink", closes.isoformat(),
         (closes - timedelta(minutes=5)).isoformat(), "[]", "e" * 64))
    conn.execute(
        "INSERT INTO rival_polymarket_model_forward (slug,model_version,issued_at,"
        "weights_sha256,probability_up,market_baseline) VALUES (?,?,?,?,?,?)",
        (slug, "pooled:" + family,
         (closes - timedelta(minutes=5, seconds=-3 if family == "a" else -9)).isoformat(),
         "w" * 64, p, up / (up + down)))
    conn.execute("INSERT INTO rival_polymarket_pooled_quotes (slug,up_ask,down_ask) "
                 "VALUES (?,?,?) ON CONFLICT DO NOTHING", (slug, up, down))
    if settled:
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes (slug,condition_id,direction,"
            "resolved_at,observed_at,receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (slug, "0x" + "d" * 64, outcome, closes.isoformat(),
             (closes + timedelta(minutes=1)).isoformat(), "r" * 64, "{}"))


def test_bet_needs_model_chance_above_price_plus_fee():
    assert implied_bet(0.60, 0.50, 0.51)[0] == "UP"
    assert implied_bet(0.40, 0.50, 0.51)[0] == "DOWN"
    assert implied_bet(0.52, 0.50, 0.51) is None  # inside the 2¢ + fee margin


def test_locked_until_enough_profitable_bets_and_first_call_only(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            for i in range(200):  # coin-flip results: should never unlock
                _contract(conn, i, p=0.60, outcome="UP" if i % 2 else "DOWN")
            # A second model family that would have been right is ignored.
            _contract(conn, 0, p=0.40, outcome="DOWN", family="b")
        record = early_call_record(store, asset="BTC", horizon=5, now=NOW)
        assert record["bets"] == 200 and record["wins"] == 100
        assert not record["unlocked"] and record["return_per_dollar"] < 0
        assert "LOCKED" in early_call_text(record)
    finally:
        store.close()


def test_unlocks_only_past_the_preregistered_bar(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            for i in range(MIN_BETS):  # 70% winners at ~51¢
                _contract(conn, i, p=0.70, outcome="DOWN" if i % 10 < 3 else "UP",
                          closes=NOW - timedelta(seconds=30 * (i + 1)))
            _contract(conn, 99999, p=0.70, outcome=None, settled=False,
                      closes=NOW + timedelta(minutes=4))
        record = early_call_record(store, asset="BTC", horizon=5, now=NOW)
        assert record["bets"] == MIN_BETS and record["unlocked"]
        assert "UNLOCKED" in early_call_text(record)
        live = current_early_call(store, asset="BTC", horizon=5, slug="btc-updown-5m-99999")
        assert live["side"] == "UP" and 0.5 < live["cost"] < 0.52
    finally:
        store.close()


def test_price_check_line():
    def call(direction, p):
        return ContractCall(slug="s", asset="BTC", horizon=5, issued_at=NOW,
                            expires=NOW + timedelta(minutes=5), direction=direction,
                            model="m", thesis="t", risk="r", evidence_sha256="e",
                            kind="model", probability_up=p)
    expensive = value_line(call("UP", 0.559), SimpleNamespace(up_ask=0.66, down_ask=0.35))
    assert "costs 67¢" in expensive and "skip at this price" in expensive
    cheap = value_line(call("DOWN", 0.436), SimpleNamespace(up_ask=0.52, down_ask=0.49))
    assert "DOWN 56%" in cheap and "cheaper than the model's own chance" in cheap
    assert value_line(call("WAIT", 0.5), SimpleNamespace(up_ask=0.5, down_ask=0.5)) == ""
