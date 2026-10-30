"""Startup crash regression for the shared XRP research/history asset catalog.

Only constructors and mocked historical data are exercised. No exchange
requests, Discord gateway connections, real wagers, or synthetic live labels.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals.bitget_history_recovery import BitgetHistoryRecovery
from parlay_bot.perpetuals.deribit_history_recovery import DeribitHistoryRecovery
from parlay_bot.perpetuals.dydx_history_recovery import DydxHistoryRecovery
from parlay_bot.perpetuals.history_inventory import history_inventory, six_months_prior
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.hyperliquid_history_recovery import HyperliquidHistoryRecovery
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.lighter_history_recovery import LighterHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
from parlay_bot.perpetuals.phemex_history_recovery import PhemexHistoryRecovery
from parlay_bot.storage.hot import SQLiteHotStore

RECOVERY_TYPES = (
    GateHistoryRecovery, KucoinHistoryRecovery, BitgetHistoryRecovery,
    OkxHistoryRecovery, HyperliquidHistoryRecovery, DeribitHistoryRecovery,
    LighterHistoryRecovery, DydxHistoryRecovery, PhemexHistoryRecovery,
)
CLOCK = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_all_nine_recovery_workers_accept_configured_xrp(tmp_path):
    hot = SQLiteHotStore(tmp_path / "constructors.sqlite")
    market = PerpetualMarketStore(hot)
    workers = []
    try:
        assert ASSETS == ("BTC", "ETH", "SOL", "XRP")
        for cls in RECOVERY_TYPES:
            worker = cls(market, assets=ASSETS)
            workers.append(worker)
            assert worker.assets == ASSETS
        for cls in RECOVERY_TYPES:
            with pytest.raises(ValueError, match="invalid"):
                cls(market, assets=("BTC", "DOGE"))
        assert len(workers) == 9
    finally:
        for worker in workers:
            await worker.aclose()
        hot.close()


@pytest.mark.asyncio
async def test_xrp_gate_backfill_and_six_month_audit_keep_true_provenance(tmp_path):
    hot = SQLiteHotStore(tmp_path / "xrp-audit.sqlite")
    market = PerpetualMarketStore(hot)
    cursor = six_months_prior(CLOCK).replace(
        minute=0, second=0, microsecond=0,
    )
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert "authorization" not in request.headers
        assert request.url.host == "api.gateio.ws"
        if request.url.path.endswith("/contracts/XRP_USDT"):
            return httpx.Response(200, json={
                "name": "XRP_USDT", "type": "direct", "status": "trading",
                "in_delisting": False, "quanto_multiplier": "1",
            })
        assert request.url.path.endswith("/candlesticks")
        assert request.url.params["contract"] == "XRP_USDT"
        assert request.url.params["interval"] == "5m"
        assert int(request.url.params["from"]) == int(cursor.timestamp())
        return httpx.Response(200, json=[{
            "t": int((cursor + i * timedelta(minutes=5)).timestamp()),
            "o": "2", "h": "2.5", "l": "1.5", "c": "2.1", "v": "20",
        } for i in range(12)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    worker = GateHistoryRecovery(
        market, assets=("XRP",), page_bars=12, client=client,
    )
    try:
        assert await worker.run_once(now=CLOCK) == 12
        assert len(seen) == 2
        rows = market.recent_candles(
            venue="gate", contract="XRP_USDT", quote="USDT",
            interval_minutes=5, count=12,
        )
        assert len(rows) == 12
        assert datetime.fromisoformat(rows[0]["first_observed_at"]) > cursor
        inventory = history_inventory(
            market, asset="XRP", venues=("gate",), now=CLOCK,
        )
        assert inventory["saved"] == 12
        assert inventory["backfill_is_live_data"] is False
        audit = SixMonthQualityAudit(market).audit_stream(
            venue="gate", asset="XRP", now=CLOCK,
        )
        assert audit["valid_bars"] == 12
        assert audit["status"] == "gaps_or_anomalies"
    finally:
        await worker.aclose()
        await client.aclose()
        hot.close()


@pytest.mark.asyncio
async def test_postgres_rival_boot_with_default_four_assets_and_recovery():
    """Catch the production crash before bot.login(), under real PostgreSQL."""
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres 16 CI service unavailable")
    settings = Settings(
        _env_file=None,
        rival_storage_backend="postgres",
        rival_database_url=SecretStr(dsn),
        rival_perp_collection_enabled=True,
        rival_history_recovery_enabled=True,
        rival_perp_collection_assets="BTC,ETH,SOL,XRP",
        rival_archive_autostart_when_configured=False,
    )
    bot = RivalDiscordClient(settings)
    try:
        assert bot.perp_collector is not None
        assert bot.perp_collector.assets == ASSETS
        workers = (
            bot.history_recovery, bot.kucoin_history_recovery,
            bot.bitget_history_recovery, bot.okx_history_recovery,
            bot.hyperliquid_history_recovery, bot.deribit_history_recovery,
            bot.lighter_history_recovery, bot.dydx_history_recovery,
            bot.phemex_history_recovery,
        )
        assert len(workers) == 9
        assert all(w is not None and w.assets == ASSETS for w in workers)
    finally:
        await bot.close()
