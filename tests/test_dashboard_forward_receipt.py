"""Tests the public-safe, read-only latest prospective forecast receipt."""
from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.dashboard_forward_receipt import (
    MAX_ITEMS,
    _row,
    read_forward_calls,
)

NOW = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)


def sample(now=NOW):
    opened = now - timedelta(minutes=2)
    return {
        "asset": "BTC", "horizon_minutes": 5,
        "venue": "okx", "quote": "USDT",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "target_opened_at": opened.isoformat(),
        "probability_up": 0.64, "baseline_up": 0.53,
        "settled_at": None, "outcome_up": None,
        "weights_sha256": "PRIVATE", "contract": "PRIVATE",
    }


class _Conn:
    def __init__(self, row):
        self.row = row
        self.sql: list[str] = []

    def execute(self, sql, params):
        self.sql.append(sql)
        assert sql.lstrip().upper().startswith("SELECT ")
        assert "ORDER BY issued_at DESC LIMIT 1" in sql
        assert len(params) == 5
        assert params[2] == "prospective-logistic-persistent-v3"
        asset, horizon = params[0], params[1]

        class _Result:
            def fetchone(_self):
                return self.row if self.row and (asset, horizon) == ("BTC", 5) else None
        return _Result()


class _Store:
    def __init__(self, row):
        self.conn = _Conn(row)

    def connect(self):
        class _Ctx:
            def __enter__(_self):
                return self.conn

            def __exit__(_self, kind, value, tb):
                return False
        return _Ctx()


class ForwardDashboardReceiptTests(unittest.TestCase):
    def test_real_issued_forecast_and_all_scope_bounded_reads(self):
        store = _Store(sample())
        result = read_forward_calls(store, NOW)
        self.assertEqual(result["status"], "observed")
        self.assertTrue(result["not_trading_calls"])
        self.assertEqual(len(store.conn.sql), MAX_ITEMS)
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["direction"], "UP")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["probability_up"], 0.64)
        self.assertEqual(row["baseline_up"], 0.53)
        self.assertNotIn("contract", row)
        self.assertNotIn("weights_sha256", row)
        self.assertEqual(row["lead_seconds"], 240)

    def test_causality_issuance_after_close_rejected(self):
        row = sample()
        row["target_opened_at"] = (NOW - timedelta(minutes=10)).isoformat()
        self.assertIsNone(_row(row, asset="BTC", horizon=5, now=NOW))

    def test_future_issue_does_not_become_live(self):
        row = sample()
        row["issued_at"] = (NOW + timedelta(seconds=5)).isoformat()
        self.assertIsNone(_row(row, asset="BTC", horizon=5, now=NOW))

    def test_missing_and_nan_probabilities_cannot_publish(self):
        for bad in (-1, 2, float("nan"), None, "not-probability"):
            with self.subTest(bad=bad):
                row = sample()
                row["probability_up"] = bad
                self.assertIsNone(_row(row, asset="BTC", horizon=5, now=NOW))

    def test_invalid_source_identifiers_rejected(self):
        row = sample()
        row["venue"] = "okx/private?api_token=PRIVATE"
        self.assertIsNone(_row(row, asset="BTC", horizon=5, now=NOW))

    def test_settlement_is_not_inferred_before_close(self):
        row = sample()
        row["target_opened_at"] = (NOW - timedelta(minutes=7)).isoformat()
        row["issued_at"] = (NOW - timedelta(minutes=6)).isoformat()
        result = _row(row, asset="BTC", horizon=5, now=NOW)
        self.assertEqual(result["status"], "awaiting_settlement")
        row["outcome_up"] = 1
        row["settled_at"] = (NOW - timedelta(seconds=45)).isoformat()
        result = _row(row, asset="BTC", horizon=5, now=NOW)
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["outcome_up"], 1)
        row["settled_at"] = (NOW - timedelta(minutes=3)).isoformat()
        self.assertIsNone(_row(row, asset="BTC", horizon=5, now=NOW))

    def test_no_trade_candle_remains_issued_but_ungraded(self):
        row = sample()
        row["target_opened_at"] = (NOW - timedelta(minutes=7)).isoformat()
        row["issued_at"] = (NOW - timedelta(minutes=6)).isoformat()
        row["settled_at"] = (NOW - timedelta(minutes=1)).isoformat()
        row["outcome_up"] = None
        result = _row(row, asset="BTC", horizon=5, now=NOW)
        self.assertEqual(result["status"], "ungraded")
        self.assertIsNone(result["outcome_up"])
        row["settled_at"] = (NOW - timedelta(minutes=3)).isoformat()
        self.assertIsNone(_row(row, asset="BTC", horizon=5, now=NOW))

    def test_missing_table_or_connection_is_unknown_not_zero(self):
        class BadStore:
            def connect(self):
                raise RuntimeError("postgres disconnected")
        result = read_forward_calls(BadStore(), NOW)
        self.assertEqual(result["status"], "read_unavailable")
        self.assertEqual(result["rows"], [])

    def test_no_recent_issued_row_is_not_table_failure(self):
        result = read_forward_calls(_Store(None), NOW)
        self.assertEqual(result["status"], "no_recent_forecasts")
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
