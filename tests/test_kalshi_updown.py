from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.kalshi_updown import KalshiUpDown, kalshi_fee
from parlay_bot.storage.hot import SQLiteHotStore


def test_kalshi_taker_fee_rounds_up_to_a_cent():
    assert kalshi_fee(0.50) == 0.02  # 0.07 * .25 = 0.0175 -> 2 cents
    assert kalshi_fee(0.90) == 0.01  # 0.0063 -> 1 cent
    assert kalshi_fee(0.30) == 0.02  # 0.0147 -> 2 cents


class FakeForecaster:
    def forecast(self, *, asset, horizon, opens_at, now):
        return {"probability": 0.70, "family": "pooled_15m_x", "model_sha256": "a" * 64,
                "venues": 4}


class FakeKalshi(KalshiUpDown):
    def __init__(self, store, now):
        super().__init__(store, FakeForecaster())
        self.now = now
        self.result = None

    async def _get(self, path, params=None):
        opens = self.now - timedelta(minutes=1)
        ticker = f"{params['series_ticker']}-T1" if params else ""
        if path.endswith("/orderbook"):
            # YES bids at 0.45 -> NO ask 0.55; NO bids at 0.50 -> YES ask 0.50
            return {"orderbook_fp": {"yes_dollars": [["0.45", "10"]],
                                     "no_dollars": [["0.50", "10"]]}}
        if params["status"] == "open":
            return {"markets": [{"ticker": ticker, "open_time": opens.isoformat(),
                                 "close_time": (opens + timedelta(minutes=15)).isoformat(),
                                 "floor_strike": 100.0}]}
        return {"markets": [{"ticker": ticker, "result": self.result,
                             "expiration_value": "101"}]}


@pytest.mark.asyncio
async def test_records_forecast_with_real_asks_then_grades_after_fees(tmp_path, monkeypatch):
    monkeypatch.setattr("parlay_bot.perpetuals.kalshi_updown.START_PRICE_FROZEN_FROM",
                        "2000-01-01T00:00:00+00:00")
    store = SQLiteHotStore(tmp_path / "kalshi.sqlite")
    try:
        now = datetime.now(UTC)
        tracker = FakeKalshi(store, now)
        assert await tracker.issue(now) == 4  # one contract per coin
        assert await tracker.issue(now) == 0  # recorded once only
        tracker.result = "yes"
        assert await tracker.settle() == 4
        summary = tracker.summary(asset="BTC")
        assert summary["contracts"] == 1
        # Bought YES at 0.50 + 2c fee; model said 0.70 -> value bet, won.
        assert summary["value_bets"] == 1 and summary["value_bet_wins"] == 1
        assert summary["return_per_dollar_after_fees"] == pytest.approx((1 - 0.52) / 0.52)
        assert summary["model_brier"] == pytest.approx(0.09)
    finally:
        store.close()


class LateForecaster:
    def __init__(self):
        self.calls = 0

    def forecast(self, *, asset, horizon, opens_at, now):
        self.calls += 1
        return None if self.calls <= 4 else {"probability": 0.6, "family": "f",
                                              "model_sha256": "b" * 64, "venues": 3}


@pytest.mark.asyncio
async def test_waits_for_the_opening_bar_instead_of_recording_no_forecast(tmp_path):
    store = SQLiteHotStore(tmp_path / "late.sqlite")
    try:
        now = datetime.now(UTC)
        tracker = FakeKalshi(store, now)
        tracker.forecaster = LateForecaster()
        assert await tracker.issue(now) == 0  # bar not ready: nothing recorded yet
        assert await tracker.issue(now) == 4  # next minute: forecast exists
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM rival_kalshi_updown "
                                "WHERE probability_up IS NOT NULL").fetchone()[0] == 4
    finally:
        store.close()


@pytest.mark.asyncio
async def test_price_seen_too_late_is_never_compared(tmp_path):
    store = SQLiteHotStore(tmp_path / "late-price.sqlite")
    try:
        now = datetime.now(UTC)
        tracker = FakeKalshi(store, now)  # contract opened 1 min before `now`
        await tracker.issue(now + timedelta(minutes=2))  # first look 3 min in
        with store.connect() as conn:
            row = conn.execute("SELECT yes_ask, probability_up FROM rival_kalshi_updown "
                               "LIMIT 1").fetchone()
        assert row["yes_ask"] is None and row["probability_up"] is not None
    finally:
        store.close()
