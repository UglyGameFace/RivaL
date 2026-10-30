"""Get Signal serves the promoted challenger, with disclosure, or not at all.

When a slot is promoted, the fused forecast drives the signal and the
embed names the engine. When it is not promoted, fused rows are invisible
to the signal path: the live model (or no signal) serves instead. The
tap-time venue gate applies to the challenger exactly as to the live
model: an unproven challenger shows UNVERIFIED, never WATCH.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL
from parlay_bot.perpetuals.promotion import initialize as init_promotion
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_multi_watches import seed_candle_and_feed, seed_multi_watch
from tests.test_promotion import seed_multi_settled


def seed_promotion_row(store, now):
    init_promotion(store)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_model_promotion (
               asset,horizon_minutes,engine,model_version,evaluated_at,
               unique_target_windows,calendar_days,paired_gain,
               challenger_brier,incumbent_brier,quote_windows,
               challenger_gross_bps,incumbent_gross_bps,status,reason
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "BTC",
                5,
                "multi",
                MULTI_MODEL,
                now.isoformat(),
                100,
                7,
                0.05,
                0.20,
                0.25,
                20,
                10.0,
                5.0,
                "promoted",
                "test promotion",
            ),
        )


def mock_books(now):
    book = PerpReferenceBook(
        asset="BTC",
        venue="hyperliquid",
        bid=81000,
        ask=81001,
        bid_quantity=10,
        ask_quantity=10,
        exchange_at=now,
        received_at=now,
    )
    return type(
        "Quotes",
        (),
        {
            "get_reference": AsyncMock(return_value=book),
            "get": AsyncMock(side_effect=Exception("no binance book")),
            "get_venue": AsyncMock(side_effect=Exception("no venue book")),
        },
    )()


@pytest.mark.asyncio
async def test_promoted_slot_serves_fused_watch_with_disclosure(tmp_path, caplog):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "promoted.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_candle_and_feed(market, now)
        seed_multi_watch(market, now, probability=0.68)
        seed_multi_settled(store, now, count=50, days_span=1, base_offset_days=1)
        seed_promotion_row(store, now)
        with caplog.at_level(logging.INFO, logger="parlay_bot.discord_app.signals"):
            signal = await signal_embed(
                market,
                mock_books(now),
                asset="BTC",
                horizon_minutes=5,
                now=now,
            )
        assert "UP WATCH" in signal.title
        assert "multi-fused" in next(x.value for x in signal.fields if x.name == "Model and expiry")
        assert "Source: **hyperliquid**" in next(x.value for x in signal.fields if x.name == "Model and expiry")
        assert "engine=multi" in caplog.text
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unpromoted_slot_ignores_fused_rows(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "unpromoted.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_candle_and_feed(market, now)
        seed_multi_watch(market, now, probability=0.68)
        signal = await signal_embed(
            market,
            mock_books(now),
            asset="BTC",
            horizon_minutes=5,
            now=now,
        )
        assert "NO LIVE SIGNAL" in signal.title
    finally:
        store.close()


@pytest.mark.asyncio
async def test_promoted_but_unproven_challenger_is_unverified(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "unverified.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_candle_and_feed(market, now)
        seed_multi_watch(market, now, probability=0.68)
        seed_promotion_row(store, now)
        signal = await signal_embed(
            market,
            mock_books(now),
            asset="BTC",
            horizon_minutes=5,
            now=now,
        )
        assert "UNVERIFIED" in signal.title
        assert "WATCH" not in signal.title
    finally:
        store.close()
