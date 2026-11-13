"""Run the production scoring query against both supported SQL backends."""
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.evidence_score_receipt import (
    MODEL,
    _scope_rows,
    summarize_rows,
)


def test_busy_long_horizons_cannot_starve_5m_and_future_settlements_are_excluded(learning_store):
    now = datetime(2026, 11, 13, 4, tzinfo=UTC)
    rows = []
    for horizon, count, age in ((5, 12, 12), (15, 510, 8), (60, 510, 4)):
        for i in range(count):
            issued = now - timedelta(hours=age) + timedelta(seconds=10 * i)
            target = issued + timedelta(minutes=horizon if horizon != 5 else -2)
            rows.append(("BTC", "bitget", "BTCUSDT", "USDT", horizon, MODEL,
                         issued.isoformat(), target.isoformat(),
                         (target + timedelta(minutes=6)).isoformat(), 1, .2, .3))
    # All are excluded by the actual production query, not just a formatter.
    for changes in ({8: (now + timedelta(minutes=1)).isoformat()},
                    {5: "other-model"}, {6: (datetime.fromisoformat(rows[0][7])
                                           + timedelta(minutes=5)).isoformat()},
                    {10: 2.0}, {0: "ETH"}):
        row = list(rows[0])
        for index, value in changes.items():
            row[index] = value
        rows.append(tuple(row))
    with learning_store.connect() as conn:
        conn.executescript("""
            CREATE TABLE perp_forward_audit (
                asset TEXT,venue TEXT,contract TEXT,quote TEXT,horizon_minutes INTEGER,
                model_version TEXT,issued_at TEXT,target_opened_at TEXT,settled_at TEXT,
                outcome_up INTEGER,model_brier DOUBLE PRECISION,baseline_brier DOUBLE PRECISION);
            CREATE INDEX scope_score ON perp_forward_audit(asset,horizon_minutes,issued_at);
        """)
        for offset in range(0, len(rows), 100):
            batch = rows[offset:offset + 100]
            conn.execute("INSERT INTO perp_forward_audit VALUES "
                         + ",".join("(" + ",".join("?" for _ in r) + ")" for r in batch),
                         tuple(value for row in batch for value in row))
    for horizon, expected in ((5, 12), (15, 500), (60, 500)):
        with learning_store.connect() as conn:
            result = _scope_rows(conn, "BTC", horizon, now)
        assert len(result) == expected
        summary = summarize_rows("BTC", result)[0]
        assert summary["horizon"] == horizon
        assert summary["model_brier"] == .2
        assert summary["baseline_brier"] == .3
        assert summary["unique_target_windows"] == expected
        assert summary["venue_count"] == 1
        assert summary["sampling_policy"] == "per-asset-horizon-close-v3"
        assert summary["within_target_candle_pairs"] == (12 if horizon == 5 else 0)
        assert summary["after_cost_qualified"] is False


def test_five_minute_call_before_close_is_prospective_with_actual_lead_time():
    row = {"venue": "bitget", "contract": "BTCUSDT", "quote": "USDT",
           "horizon_minutes": 5, "issued_at": "2026-11-13T03:02:00+00:00",
           "target_opened_at": "2026-11-13T03:00:00+00:00",
           "settled_at": "2026-11-13T03:06:00+00:00",
           "model_brier": .2, "baseline_brier": .3}
    summary = summarize_rows("BTC", [row])[0]
    assert summary["paired_outcomes"] == 1
    assert summary["within_target_candle_pairs"] == 1
    assert summary["minimum_lead_seconds"] == 180
    assert summary["outcome_clock"] == "target_5m_candle_close"
    assert summarize_rows("BTC", [{**row, "issued_at": "2026-11-13T03:05:00+00:00"}]) == []
    assert summarize_rows("BTC", [{**row, "settled_at": "2026-11-13T03:04:59+00:00"}]) == []
    assert summarize_rows("BTC", [{**row, "issued_at": "2026-11-13T03:02:00"}]) == []
    # Offsets are compared as instants, not as lexically ordered strings.
    equivalent = {**row, "issued_at": "2026-11-12T22:02:00-05:00"}
    assert summarize_rows("BTC", [equivalent])[0]["minimum_lead_seconds"] == 180
