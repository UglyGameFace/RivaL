"""Kalshi learns only from its own first market quotes and official YES/NO receipts."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.kalshi_public import KalshiCryptoBrowser, KalshiMarket
from parlay_bot.perpetuals.kalshi_forward import KalshiForwardResearch
from parlay_bot.storage.hot import SQLiteHotStore


def market(i: int, at: datetime, *, asset="BTC", series="KXBTC15M"):
    return KalshiMarket(
        ticker=f"{series}-{i}", title=f"BTC test contract {i}",
        close_time=at+timedelta(minutes=15),
        yes_ask=.52 if i % 2 else .47,
        no_ask=.51 if i % 2 else .58,
        rules="BTC Crypto price settlement according to Kalshi's rule",
        series_ticker=series,
    )


def receipt(mkt, result, settled):
    return {"market": {
        "ticker": mkt.ticker, "status": "settled",
        "result": result, "settlement_ts": settled.isoformat(),
    }}


def test_causal_kalshi_shadow_trains_only_on_past_settlements(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "kalshi.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(100):
        at = start+timedelta(minutes=i * 30)
        m = market(i, at)
        assert ledger.observe(asset="BTC", market=m, at=at)
        assert not ledger.observe(asset="BTC", market=m, at=at)
        assert ledger.settle(
            ticker=m.ticker, payload=receipt(m,"yes" if i % 2 else "no",
                                             at+timedelta(minutes=20)),
            checked_at=at+timedelta(minutes=21),
        )
    status = ledger.summary(asset="BTC")
    assert status["trained_labels"] == 100
    assert status["candidate_forward_resolved"] == 0
    assert status["approved_model"] is False
    # After 100 observed, already-settled and verified labels, a candidate
    # may be frozen for a NEW market but is still research only.
    at = start+timedelta(minutes=100*30)
    m = market(100, at)
    assert ledger.observe(asset="BTC", market=m, at=at)
    with ledger.store.connect() as db:
        frozen = db.execute(
            "SELECT candidate_yes,candidate_weights_sha256,"
            "candidate_training_labels FROM rival_kalshi_forward WHERE ticker=?",
            (m.ticker,),
        ).fetchone()
    assert 0 < frozen["candidate_yes"] < 1
    assert frozen["candidate_weights_sha256"]
    assert frozen["candidate_training_labels"] == 100
    assert ledger.settle(
        ticker=m.ticker, payload=receipt(m, "no", at+timedelta(minutes=20)),
        checked_at=at+timedelta(minutes=21),
    )
    result = ledger.summary(asset="BTC")
    assert result["candidate_forward_resolved"] == 1
    assert result["candidate_brier"] is not None
    assert result["kalshi_quote_baseline_brier"] is not None
    assert result["verified_betting_advantage"] is False
    assert result["approved_model"] is False
    with ledger.store.connect() as db:
        after = db.execute(
            "SELECT candidate_yes,candidate_weights_sha256,"
            "candidate_training_labels FROM rival_kalshi_forward WHERE ticker=?",
            (m.ticker,),
        ).fetchone()
    assert tuple(after) == tuple(frozen)
    assert not ledger.settle(
        ticker=m.ticker, payload=receipt(m, "no", at+timedelta(minutes=20)),
        checked_at=at+timedelta(minutes=21),
    )
    with pytest.raises(ValueError, match="changed"):
        ledger.settle(
            ticker=m.ticker, payload=receipt(m, "yes", at+timedelta(minutes=20)),
            checked_at=at+timedelta(minutes=22),
        )


def test_kalshi_series_models_never_transfer_training_labels(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "scope.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    first = market(1, start)
    ledger.observe(asset="BTC", market=first, at=start)
    ledger.settle(
        ticker=first.ticker,
        payload=receipt(first,"yes",start+timedelta(minutes=20)),
        checked_at=start+timedelta(minutes=21),
    )
    other = market(2,start+timedelta(minutes=30),series="KXBTC1H")
    assert ledger.observe(asset="BTC", market=other,
                          at=start+timedelta(minutes=30))
    with ledger.store.connect() as conn:
        counts = conn.execute(
            "SELECT series_ticker,updates FROM rival_kalshi_shadow_weights "
            "ORDER BY series_ticker"
        ).fetchall()
        proof = conn.execute(
            "SELECT candidate_yes FROM rival_kalshi_forward WHERE ticker=?",
            (other.ticker,),
        ).fetchone()
    assert [(x["series_ticker"],x["updates"]) for x in counts] == [
        ("KXBTC15M",1),("KXBTC1H",0),
    ]
    assert proof["candidate_yes"] is None


def test_incomplete_kalshi_source_or_settlement_fails_closed(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "reject.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    m = market(3,start)
    with pytest.raises(ValueError):
        ledger.observe(asset="BTC", market=KalshiMarket(
            ticker="KXBTC15M-3", title="invalid", close_time=m.close_time,
            yes_ask=.5, no_ask=.5, rules="statement", series_ticker=None,
        ),at=start)
    with pytest.raises(ValueError, match="BOTH real ask"):
        ledger.observe(asset="BTC", market=KalshiMarket(
            ticker=m.ticker,title=m.title,close_time=m.close_time,
            yes_ask=None,no_ask=.5,rules=m.rules,series_ticker=m.series_ticker,
        ),at=start)
    assert ledger.observe(asset="BTC", market=m, at=start)
    pending = ledger.pending(now=start+timedelta(minutes=16))
    assert pending == (m.ticker,)
    for invalid in (
        {"market":{"ticker":m.ticker,"status":"open","result":"yes"}},
        {"market":{"ticker":m.ticker,"status":"settled","result":"void"}},
        {"market":{"ticker":m.ticker,"status":"settled","result":"yes"}},
        receipt(m,"yes",start+timedelta(minutes=10)),
    ):
        assert not ledger.settle(
            ticker=m.ticker,payload=invalid,checked_at=start+timedelta(minutes=30),
        )
    assert ledger.summary(asset="BTC")["trained_labels"] == 0


@pytest.mark.asyncio
async def test_kalshi_browser_records_official_market_without_user_messages(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "browse.sqlite"))
    def respond(req):
        if req.url.path.endswith("/series"):
            return httpx.Response(200,json={"series":[{
                "ticker":"KXBTC15M","title":"Bitcoin 15m",
                "category":"Crypto",
            }]})
        if req.url.path.endswith("/markets"):
            return httpx.Response(200,json={"markets":[{
                "ticker":"KXBTC15M-ABC","title":"BTC above X",
                "status":"open",
                "close_time":(datetime.now(UTC)+timedelta(minutes=15)).isoformat(),
                "rules_primary":"Kalshi BTC settlement",
                "yes_ask_dollars":"0.52","no_ask_dollars":"0.51",
            }]})
        raise AssertionError("No settlement query before market close")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        browser = KalshiCryptoBrowser(client=client,forward=ledger)
        found = await browser.browse("BTC")
        assert found.markets[0].series_ticker == "KXBTC15M"
        if browser._study_tasks:
            import asyncio
            await asyncio.gather(*browser._study_tasks)
        assert ledger.summary(asset="BTC")["observed"] == 1
        await browser.aclose()
