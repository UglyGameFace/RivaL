"""Math & History: settled Polymarket results and price fairness, from receipts only."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.polymarket_history import (
    fairness_text,
    polymarket_history,
    results_text,
)
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 6, 6, tzinfo=UTC)


def _settled(store, i, *, up_price, outcome, horizon=5, settled=True):
    closes = NOW - timedelta(minutes=5 * i)
    slug = f"btc-updown-{horizon}m-{i}"
    up, down = up_price, 1 - up_price
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_observations (slug,condition_id,asset,horizon,"
            "resolution_source,expires_at,observed_at,features_json,evidence_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, "0x" + "c" * 64, "BTC", horizon, "chainlink", closes.isoformat(),
             (closes - timedelta(minutes=4)).isoformat(),
             json.dumps([up, down, up / (up + down), 0, 0, 0, 1, 0, up - down, 0]), "e" * 64))
        if settled:
            conn.execute(
                "INSERT INTO rival_polymarket_ai_outcomes (slug,condition_id,direction,"
                "resolved_at,observed_at,receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?)",
                (slug, "0x" + "c" * 64, outcome, closes.isoformat(),
                 (closes + timedelta(minutes=1)).isoformat(), "r" * 64, "{}"))


def test_results_and_fairness_come_only_from_settled_receipts(tmp_path):
    store = SQLiteHotStore(tmp_path / "history.sqlite")
    try:
        PolymarketOutcomeLearner(store).initialize()
        PolymarketResearch(store).initialize()
        # 40 near-even contracts: 20 UP, 20 DOWN (fair). Newest (i=1..3) are DOWN.
        for i in range(1, 41):
            _settled(store, i, up_price=0.5, outcome="DOWN" if i <= 20 else "UP")
        # 30 contracts priced UP at 80¢ where UP always won (more than priced).
        for i in range(41, 71):
            _settled(store, i, up_price=0.8, outcome="UP")
        _settled(store, 0, up_price=0.5, outcome="UP", settled=False)  # unsettled: ignored
        history = polymarket_history(store, asset="BTC", now=NOW)
        five = history["horizons"][5]
        assert len(five["results"]) == 70 and five["results"][-1] is False
        text = results_text(history)
        assert "70 settled" in text and "UP **71%**" in text
        assert "20 DOWN in a row" in text and text.count("🔴") == 13  # 12 results + the legend
        fair = fairness_text(history)
        assert "45–55¢" in fair and "≈ fair" in fair
        assert "over 70¢" in fair and "UP won more than priced" in fair
        assert "differed from the price by more than normal chance" in fair
    finally:
        store.close()
