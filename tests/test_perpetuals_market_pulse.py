"""Five-minute observed market pulse, never a forward betting guarantee."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_pulse import calculate_market_pulse
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

VENUES = (
    ("hyperliquid", "BTC", "USDC", 1),
    ("dydx", "BTC-USD", "USD", -1),
    ("bitget", "BTCUSDT", "USDT", 0),
    ("gate", "BTC_USDT", "USDT", 1),
)


def seed(market: PerpetualMarketStore, *, moment: datetime, count: int = 13) -> None:
    cutoff = moment.replace(second=0, microsecond=0)
    cutoff = cutoff.replace(minute=cutoff.minute // 5 * 5)
    for venue, contract, quote, direction in VENUES:
        rows = []
        for i in range(count):
            opened = cutoff - timedelta(minutes=5 * (count - i))
            close = 100 + direction * i / 10
            rows.append(MarketCandle(
                venue, contract, quote, 5, opened,
                opened + timedelta(minutes=5) - timedelta(milliseconds=1),
                cutoff, close, close + 5, close - 5, close,
                1.0, 10,
            ))
        assert market.ingest(
            MarketBatch((), tuple(rows), 5, cutoff)
        ).inserted_candles == count
        market.record_feed_health(
            venue=venue, asset="BTC", status="ok", attempted_at=cutoff,
            new_candles=count, total_candles=count,
        )


def test_pulse_uses_all_venues_5m_change_not_future_trade_odds(tmp_path):
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime(2026, 10, 28, 14, 35, tzinfo=UTC)
    try:
        seed(market, moment=now)
        result = calculate_market_pulse(
            market, asset="BTC", venues=tuple(v for v, *_ in VENUES), now=now,
        )
        assert result.sources == 4
        assert result.configured == 4
        assert result.up == 2 and result.down == 1 and result.flat == 1
        assert result.median_5m_pct > 0
        assert result.range_5m_pct[0] < 0 < result.range_5m_pct[1]
        assert result.median_15m_pct is not None
        assert result.median_1h_pct is not None
        assert result.horizon_minutes == 5
        assert result.model_edge_verified is False
        assert not hasattr(result, "take_profit")
        evidence, _ = build_evidence(
            market, asset="BTC", venues=tuple(v for v, *_ in VENUES), now=now,
        )
        assert evidence["horizon_minutes"] == 5
        assert all("5m" in venue["windows"] for venue in evidence["sources"])
    finally:
        store.close()


def test_pulse_rejects_stale_sources_and_needs_three_distinct_venues(tmp_path):
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    market = PerpetualMarketStore(store)
    now = datetime(2026, 10, 28, 14, 35, tzinfo=UTC)
    try:
        seed(market, moment=now)
        result = calculate_market_pulse(
            market, asset="BTC",
            venues=("hyperliquid", "dydx", "bitget"), now=now,
        )
        assert result.sources == 3
        with pytest.raises(InsufficientEvidence, match="three"):
            calculate_market_pulse(
                market, asset="BTC", venues=("hyperliquid", "dydx"), now=now,
            )
        with pytest.raises(InsufficientEvidence, match="three"):
            calculate_market_pulse(
                market, asset="BTC", venues=tuple(v for v, *_ in VENUES),
                now=now + timedelta(hours=1),
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_math_screen_labels_past_5m_move_not_validated_bet(tmp_path):
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    market = PerpetualMarketStore(store)
    seed(market, moment=datetime.now(UTC))
    try:
        view = ResearchDashboard(
            owner_id=42, market=market, ai=None,
            venues=tuple(v for v, *_ in VENUES),
        )
        embed = await view.render()
        fields = {field.name: field.value for field in embed.fields}
        pulse = fields["Current market pulse · NOT a trading signal"]
        assert "4/4" in pulse
        assert "Previous 5m median move" in pulse
        assert "15m context" in pulse
        assert "Next 5m forecast: NOT VALIDATED" in pulse
        assert "Past momentum is not the next candle" in pulse
    finally:
        store.close()


@pytest.mark.asyncio
async def test_math_screen_falls_back_to_no_signal_if_sources_stale(tmp_path):
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    try:
        view = ResearchDashboard(
            owner_id=42, market=PerpetualMarketStore(store), ai=None,
            venues=("hyperliquid", "dydx", "bitget"),
        )
        embed = await view.render()
        fields = {field.name: field.value for field in embed.fields}
        assert "NO SIGNAL" in fields["Current market pulse"]
    finally:
        store.close()
