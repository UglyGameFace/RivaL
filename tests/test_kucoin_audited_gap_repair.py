"""KuCoin history repairs only audit-proven absent source candles."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

CLOCK = datetime(2026, 10, 31, 12, tzinfo=UTC)
END = CLOCK - timedelta(hours=4)
FIRST = END - timedelta(minutes=15)
HOLE = FIRST + timedelta(minutes=5)


def _seed(tmp_path, monkeypatch):
    import parlay_bot.perpetuals.history_quality as quality

    # Exercise exact gap discovery with three timestamps, without creating
    # fifty thousand bars in the unit test. Real code uses six calendar months.
    monkeypatch.setattr(quality, "six_months_prior",
                        lambda end: end - timedelta(minutes=15))
    store = SQLiteHotStore(tmp_path / "kucoin-holes.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    for stamp in (FIRST, FIRST + timedelta(minutes=10)):
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_market_candles (
                   venue,contract,quote,interval_minutes,opened_at,closed_at,
                   first_observed_at,open_price,high_price,low_price,close_price,
                   volume_base,trades)
                   VALUES ('kucoin','XBTUSDTM','USDT',5,?,?,?,?,?,?,?,?,?)""",
                (
                    stamp.isoformat(),
                    (stamp+timedelta(minutes=5, milliseconds=-1)).isoformat(),
                    END.isoformat(), 100, 110, 90, 105, 1, -1,
                ),
            )
    worker = KucoinHistoryRecovery(market, assets=("BTC",), page_bars=12)
    worker.initialize()
    worker._position("BTC", CLOCK)
    with store.connect() as conn:
        conn.execute(
            """UPDATE perp_history_recovery SET next_opened_at=?,status='caught_up'
               WHERE venue='kucoin' AND asset='BTC'""",
            (END.isoformat(),),
        )
    auditor = SixMonthQualityAudit(market)
    return store, market, worker, auditor


@pytest.mark.asyncio
async def test_audited_gap_repairs_one_exact_provider_candle_without_rewind(
    tmp_path, monkeypatch,
):
    store, _market, worker, auditor = _seed(tmp_path, monkeypatch)
    calls = []

    async def multiplier(asset, symbol, now):
        return 1.0

    async def public(url, params):
        calls.append(params)
        return {"code": "200000", "data": {
            "symbol": "XBTUSDTM", "tradeType": "FUTURES",
            "list": [[int(HOLE.timestamp()), "100", "110", "90", "105", "1", "0"]],
        }}

    worker._multiplier = multiplier
    worker._public = public
    try:
        result = auditor.audit_stream(venue="kucoin", asset="BTC", now=END)
        assert result["missing_bars"] == 1
        auditor.audit_stream(venue="kucoin", asset="BTC", now=END)
        assert worker._pending_gaps("BTC", now=CLOCK) == 1

        assert await worker.fetch_asset("BTC", now=CLOCK) == 1
        assert len(calls) == 1
        assert calls[0]["startAt"] == int(HOLE.timestamp())
        assert calls[0]["endAt"] == int(HOLE.timestamp())
        assert worker._pending_gaps("BTC", now=CLOCK) == 0

        with store.connect() as conn:
            record = conn.execute(
                "SELECT first_observed_at FROM perp_market_candles "
                "WHERE venue='kucoin' AND contract='XBTUSDTM' AND opened_at=?",
                (HOLE.isoformat(),),
            ).fetchone()
            pos = conn.execute(
                "SELECT next_opened_at FROM perp_history_recovery "
                "WHERE venue='kucoin' AND asset='BTC'",
            ).fetchone()
        assert record is not None and datetime.fromisoformat(
            record["first_observed_at"]
        ) > CLOCK
        assert pos["next_opened_at"] == END.isoformat()
    finally:
        await worker.aclose()
        store.close()


@pytest.mark.asyncio
async def test_empty_gap_retry_remains_queued_and_does_not_fake_repair(
    tmp_path, monkeypatch,
):
    store, market, worker, auditor = _seed(tmp_path, monkeypatch)
    worker._multiplier = lambda *args: None  # overwritten with async below

    async def multiplier(*_args):
        return 1.0

    async def empty(_url, _params):
        return {"code": "200000", "data": {
            "symbol": "XBTUSDTM", "tradeType": "FUTURES", "list": [],
        }}

    worker._multiplier = multiplier
    worker._public = empty
    try:
        auditor.audit_stream(venue="kucoin", asset="BTC", now=END)
        assert await worker.fetch_asset("BTC", now=CLOCK) == 0
        assert worker._pending_gaps("BTC", now=CLOCK) == 1
        assert await worker.fetch_asset("BTC", now=CLOCK+timedelta(minutes=1)) == 0
        with store.connect() as conn:
            missing = conn.execute(
                "SELECT 1 FROM perp_market_candles WHERE venue='kucoin' "
                "AND opened_at=?", (HOLE.isoformat(),),
            ).fetchone()
            status = conn.execute(
                "SELECT status,next_opened_at FROM perp_history_recovery "
                "WHERE venue='kucoin' AND asset='BTC'",
            ).fetchone()
        assert missing is None
        assert status["status"] == "gaps_pending"
        assert status["next_opened_at"] == END.isoformat()
    finally:
        await worker.aclose()
        store.close()


def test_large_incomplete_kucoin_history_is_never_mass_queued(tmp_path, monkeypatch):
    store, market, worker, auditor = _seed(tmp_path, monkeypatch)
    import parlay_bot.perpetuals.history_quality as quality
    monkeypatch.setattr(quality, "six_months_prior",
                        lambda end: end - timedelta(hours=12))
    try:
        report = auditor.audit_stream(venue="kucoin", asset="BTC", now=END)
        assert report["missing_bars"] > 32
        assert worker._pending_gaps("BTC", now=CLOCK) == 0
    finally:
        store.close()
