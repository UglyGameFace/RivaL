"""Spike conditional challenger evidence cannot borrow outcomes from the future."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.spike_challenger import (
    spike_context_at_issuance, spike_score_rows,
)
from parlay_bot.perpetuals.spike_watch import SpikeWatch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.storage.hot import SQLiteHotStore


def test_spike_provenance_only_applies_to_original_source_before_issue(tmp_path):
    store = SQLiteHotStore(tmp_path / "evidence.sqlite")
    spike = SpikeWatch(PerpetualMarketStore(store))
    spike.initialize()
    opened = datetime(2026, 11, 2, 17, tzinfo=UTC)
    detected = opened + timedelta(minutes=5)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_spike_events
              (asset,opened_at,detected_at,kind,direction,source_count,
               move_pct,volume_ratio,sources_json)
              VALUES (?,?,?,?,?,?,?,?,?)""",
            ("BTC", opened.isoformat(), detected.isoformat(),
             "price_and_volume", "up", 3, 1.2, 3.1, json.dumps([
                 {"venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT"},
                 {"venue": "gate", "contract": "BTC_USDT", "quote": "USDT"},
                 {"venue": "dydx", "contract": "BTC-USD", "quote": "USD"},
             ])),
        )
        def find(issue, venue="okx", contract="BTC-USDT-SWAP", quote="USDT"):
            return spike_context_at_issuance(
                conn, asset="BTC", venue=venue, contract=contract,
                quote=quote, opened_at=opened.isoformat(),
                issued_at=issue.isoformat(),
            )
        assert find(detected - timedelta(seconds=1)) is None
        assert find(detected) == {
            "kind": "price_and_volume", "detected_at": detected.isoformat(),
        }
        assert find(detected, venue="binance", contract="BTCUSDT") is None
        assert find(detected, contract="BTC-USDC-SWAP", quote="USDC") is None
        assert find(detected, quote="USD") is None
    store.close()


def test_spike_future_score_rejects_post_issue_event_and_preissue_settlement():
    issued = "2026-11-02T17:05:00+00:00"
    good = {
        "spike_kind": "price", "spike_detected_at": issued,
        "issued_at": issued, "settled_at": "2026-11-02T17:11:00+00:00",
        "target_opened_at": "2026-11-02T17:05:00+00:00",
        "probability_up": 0.90, "incumbent_up": 0.5,
        "outcome_up": 1,
    }
    rows = [
        good,
        {**good, "spike_detected_at": "2026-11-02T17:06:00+00:00"},
        {**good, "settled_at": "2026-11-02T17:04:00+00:00"},
        {**good, "spike_kind": None},
    ]
    result = spike_score_rows(rows)
    assert result == [good]
    proof = score_paired_forward([
        (row["target_opened_at"],
         (row["probability_up"]-row["outcome_up"])**2,
         (row["incumbent_up"]-row["outcome_up"])**2)
        for row in result
    ])
    assert proof.targets == 1
    assert proof.paired_gain == .25 - (1-.90)**2
    assert proof.status == "insufficient"
