"""Regression safety for the Oct 31 harsh audit findings."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.discord_app.cdna_ui import cdna_coin_embed
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.mark.asyncio
async def test_retired_autohunt_never_promises_subscription_or_sends_dm(tmp_path):
    bot = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"), discord_token=None,
    ))
    try:
        assert bot.auto_hunt.alerts_enabled is False
        ok, message = await bot.auto_hunt.subscribe(
            SimpleNamespace(id=911), source="forecasts",
        )
        assert not ok
        assert "retired" in message
        assert "retired" in await bot.auto_hunt.status(911)
        enabled = await bot.auto_hunt.toggle_tier(911, "qualified")
        assert not enabled[0]
        assert await bot.auto_hunt.cycle() == 0
        assert await bot.auto_hunt.live_cycle([]) == 0
    finally:
        await bot.zip_resolver.aclose()
        await bot.auto_hunt.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


def test_crypto_com_reference_never_presents_stale_bid_ask_as_tradeable():
    now = datetime.now(UTC)
    price = SimpleNamespace(
        bid=0.22, ask=0.44,
        updated_at=now - timedelta(minutes=5),
        fresh=False,
    )
    embed = cdna_coin_embed("BTC", ((
        "BTC-REFERENCE", "Bitcoin", "One Crypto.com Predictions event", price,
    ),))
    text = str(embed.to_dict())
    assert "Crypto.com Predictions" in text
    assert "NOT DraftKings" in text
    assert "Price withheld" in text
    assert "0.22" not in text and "0.44" not in text


@pytest.mark.parametrize("asset,horizon", [
    ("XRP", 5), ("XRP", 15), ("XRP", 60), ("BTC", 60),
])
def test_stream_quote_proof_supports_new_asset_horizons_without_inventing_edge(
    tmp_path, asset, horizon,
):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    try:
        market = PerpetualMarketStore(store)
        market.initialize()
        ForwardAccuracyMonitor(market).initialize()
        report = StreamShadowLedger(store).report(
            asset=asset, horizon_minutes=horizon,
        )
        assert report["asset"] == asset
        assert report["horizon_minutes"] == horizon
        assert report["status"] == "NOT_PROVEN"
        assert report["real_fills_verified"] is False
    finally:
        store.close()
