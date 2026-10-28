"""Bounded KuCoin Futures historical recovery and UI proof.

Historical rows preserve actual fetch time. No old candle can become a past
prediction or silently replace live market data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def public_reply(req: httpx.Request) -> httpx.Response:
    assert "authorization" not in req.headers
    if "/contracts/" in req.url.path:
        assert req.url.host == "api-futures.kucoin.com"
        symbol = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={
            "code": "200000", "data": {
                "symbol": symbol,
                "baseCurrency": "XBT" if symbol.startswith("XBT") else symbol[:-5],
                "quoteCurrency": "USDT", "settleCurrency": "USDT",
                "status": "Open", "expireDate": None, "isInverse": False,
                "multiplier": "0.001",
            },
        })
    assert req.url.host == "api.kucoin.com"
    assert req.url.path.endswith("/api/ua/v2/market/kline")
    assert req.url.params["tradeType"] == "FUTURES"
    assert req.url.params["klineType"] == "TRADE"
    assert req.url.params["interval"] == "5min"
    assert req.url.params["symbol"] == "XBTUSDTM"
    start = int(req.url.params["startAt"])
    end = int(req.url.params["endAt"])
    assert end - start == 11 * 300
    return httpx.Response(200, json={
        "code": "200000",
        "data": {
            "tradeType": "FUTURES", "symbol": "XBTUSDTM",
            "list": [
                [start + i * 300, "100", "110", "95", "102", "200", "20400"]
                for i in range(12)
            ],
        },
    })


@pytest.mark.asyncio
async def test_kucoin_history_cursor_continues_and_never_fakes_live_observation(
    tmp_path,
):
    store = SQLiteHotStore(tmp_path / "kucoin.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return public_reply(req)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    recovery = KucoinHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await recovery.run_once(now=NOW) == 12
        first = recovery.progress()[0]
        assert first["venue"] == "kucoin"
        assert first["inserted_bars"] == 12
        assert first["status"] == "progress"
        candles = market.recent_candles(
            venue="kucoin", contract="XBTUSDTM", quote="USDT",
            interval_minutes=5, count=12,
        )
        assert len(candles) == 12
        assert candles[0]["volume_base"] == pytest.approx(0.2)
        assert datetime.fromisoformat(candles[0]["first_observed_at"]).month == 10
        assert market.recent_candles(
            venue="kucoin", contract="XBTUSDTM", quote="USDT",
            interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
        # Same process caches safe metadata, not market candles.
        assert await recovery.run_once(now=NOW) == 12
        assert len([x for x in calls if "/contracts/" in x]) == 1
        # A fresh process starts where the durable cursor stopped.
        restarted = KucoinHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        assert await restarted.run_once(now=NOW) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        assert len([x for x in calls if "/contracts/" in x]) == 2
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_blocked_kucoin_does_not_fake_data_or_retry_during_cooldown(tmp_path):
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    def blocked(req):
        calls.append(req)
        return httpx.Response(451)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    worker = KucoinHistoryRecovery(market, assets=("BTC",), client=raw)
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["status"] == "blocked"
        assert row["next_opened_at"] == row["started_from"]
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 0
        assert len(calls) == 1
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_malformed_kucoin_history_preserves_cursor_and_fails_closed(tmp_path):
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)

    def invalid(req):
        if "/contracts/" in req.url.path:
            return public_reply(req)
        start = int(req.url.params["startAt"])
        return httpx.Response(200, json={
            "code": "200000", "data": {
                "tradeType": "FUTURES", "symbol": "XBTUSDTM",
                "list": [
                    [start - 300, "100", "110", "95", "102", "200", "20400"],
                ],
            },
        })

    raw = httpx.AsyncClient(transport=httpx.MockTransport(invalid))
    worker = KucoinHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["status"] == "retry"
        assert row["inserted_bars"] == 0
        assert row["next_opened_at"] == row["started_from"]
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_kucoin_recovery_is_bounded_by_official_200_bar_limit(tmp_path):
    store = SQLiteHotStore(tmp_path / "pages.sqlite")
    market = PerpetualMarketStore(store)
    try:
        with pytest.raises(ValueError, match="200"):
            KucoinHistoryRecovery(market, assets=("BTC",), page_bars=201)
        with pytest.raises(ValueError):
            KucoinHistoryRecovery(market, assets=("BTC", "BTC"), page_bars=100)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_kucoin_history_progress_visible_in_research_menu(tmp_path):
    store = SQLiteHotStore(tmp_path / "ui.sqlite")
    market = PerpetualMarketStore(store)
    worker = KucoinHistoryRecovery(market, assets=("BTC",), page_bars=12)
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("gate", "kucoin"), shadow_enabled=False,
    )
    try:
        worker._position("BTC", NOW)
        view.screen = "math"
        view.section = "recovery"
        view._rebuild()
        fields = {field.name: field.value for field in (await view.render()).fields}
        assert "Historical recovery progress" in fields
        assert "**KuCoin**: pending" in fields["Historical recovery progress"]
        assert "NOT past live predictions" in fields["Historical recovery progress"]
    finally:
        await worker.aclose()
        view.stop()
        store.close()
